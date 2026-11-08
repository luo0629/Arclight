"""QA-07: persisted admission fixtures exercise the real selection consumers."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_asset_selection import choose, current, restore
from test_asset_versions import private_objects, save, state, versions  # noqa: F401
from test_concurrency import require_postgres
from test_continuity_review_contracts import with_issue
from test_continuity_reviews import review_case as review_case
from test_continuity_reviews import saved
from test_file_transfers import transfer_setup as transfer_setup
from test_generation_candidates import candidates, generate, ingest
from test_generation_delivery import Provider, deliver, retry_due
from test_generation_jobs import capture_ok, payload
from test_upload_completion import complete, ticket

from arclight.application.continuity_review_store import require_review_record
from arclight.application.continuity_selection import require_shot_review
from arclight.application.human_reviews import ReviewHandler, request_review, submit_decision
from arclight.application.production_versions import bind
from arclight.core.errors import DomainError
from arclight.domain.continuity_selection import ADMISSION_RESOURCE, HUMAN_REVIEW_HANDLER
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AssetSelectionLock,
    AssetVersion,
    AuditEvent,
    GenerationAttempt,
    Shot,
    Storyboard,
    VersionBinding,
)
from arclight.providers.media_submission import MockMediaSubmissionProvider, SubmissionReceipt


def admission_fixture(client, record, *, human=False, request_id=None, changes=None, snapshot=None):
    """Fixture for QA-04's future domain output, never a production writer."""
    proof = {
        "schema_version": "continuity-admission/1",
        "review_id": str(record.review.result_id),
        "record_sha256": digest(record.model_dump(mode="json")),
        "requires_human_review": human,
        "human_review_request_id": str(request_id) if request_id else None,
        **(changes or {}),
    }
    value = {
        "schema_version": 1,
        "compatibility": "EXACT_DEFINITIONS",
        "components": [item.model_dump(mode="json") for item in record.review.versions],
        "parameters": {},
        "parameter_sha256": digest({}),
        "review_admission": proof,
        **(snapshot or {}),
    }
    with client.app.state.session_factory.begin() as db:
        bind(db, record.review.project_id, ADMISSION_RESOURCE, record.review.result_id, value)


def target_rows(client, value):
    target = value["target"]
    root = f"/api/v1/projects/{value['project_id']}/assets/{target['asset_id']}"
    asset = client.get(root).json()
    version = client.get(root + f"/versions/{target['asset_version_id']}").json()
    return asset, version


def approve(client, value):
    asset, version = target_rows(client, value)
    ready = state(client, asset, version, "READY_FOR_REVIEW")
    assert ready.status_code == 200, ready.text
    approved = state(client, asset, ready.json(), "APPROVED")
    assert approved.status_code == 200, approved.text
    return approved.json()


def effects(client):
    with client.app.state.session_factory() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (AuditEvent, AssetSelectionLock, VersionBinding)
        )


def human_request_fixture(
    client, record, *, decision="APPROVE", comment="逐图核实遮挡，接受本次例外。", changes=None
):
    target = record.review.target
    registry = {
        HUMAN_REVIEW_HANDLER: ReviewHandler(read=lambda _db, value: value, apply=lambda *_: {})
    }
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, target.asset_version_id)
        snapshot = ReviewSnapshot.model_validate(
            {
                "project_id": record.review.project_id,
                "episode_id": record.review.episode_id,
                "run_id": None,
                "thread_id": "fixture:" + str(record.review.result_id),
                "kind": "EXCEPTIONAL_SHOT",
                "object_id": record.review.result_id,
                "version_id": version.id,
                "content_sha256": version.checksum,
                "input_sha256": digest(record.model_dump(mode="json")),
                "source_version_ids": [target.storyboard_id],
                **(changes or {}),
            }
        )
        request = request_review(
            db, "service-owner", str(uuid4()), HUMAN_REVIEW_HANDLER, snapshot, registry=registry
        )
        if decision is not None:
            submit_decision(
                db,
                request.id,
                "service-owner",
                HumanDecision(
                    version_id=version.id,
                    request_sha256=request.request_sha256,
                    decision=decision,
                    comment=comment,
                ),
                registry=registry,
            )
        return request.id


def test_advisory_pass_never_grants_lifecycle_selection_or_lock(client, review_case):
    record, _ = saved(client, review_case)
    asset, version = target_rows(client, review_case)
    before = effects(client)
    for operation in (
        lambda: state(client, asset, version, "READY_FOR_REVIEW"),
        lambda: choose(client, asset, version),
    ):
        response = operation()
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    assert effects(client) == before
    assert current(client, asset)["current_version_id"] is None
    assert record.state == "CONTRACT_VALIDATED"


def test_valid_admission_still_needs_owner_approval_and_explicit_selection(client, review_case):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    asset, version = target_rows(client, review_case)
    assert choose(client, asset, version).json()["error"]["code"] == "ASSET_NOT_USABLE"
    version = approve(client, review_case)
    assert current(client, asset)["current_version_id"] is None
    selected = choose(client, current(client, asset), version)
    assert selected.status_code == 200, selected.text
    assert selected.json()["current_version_id"] == version["id"]
    assert choose(client, asset, version).json()["error"]["code"] == "VERSION_CONFLICT"
    with client.app.state.session_factory() as db:
        saved_record = require_review_record(
            db, record.review.project_id, record.review.result_id, "service-owner"
        )
        assert saved_record == record and saved_record.state == "CONTRACT_VALIDATED"


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "untrusted/2"},
        {"review_id": str(uuid4())},
        {"record_sha256": "a" * 64},
        {"requires_human_review": "false"},
        {"approved": True},
    ],
)
def test_invalid_admission_is_fail_closed_and_atomic(client, review_case, change):
    record, _ = saved(client, review_case)
    admission_fixture(client, record, changes=change)
    asset, version = target_rows(client, review_case)
    before = effects(client)
    response = state(client, asset, version, "READY_FOR_REVIEW")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVIEW_ADMISSION_INVALID"
    assert effects(client) == before


def test_wrong_definition_binding_cannot_authorize_a_valid_contract(client, review_case):
    record, _ = saved(client, review_case)
    admission_fixture(client, record, snapshot={"components": []})
    asset, version = target_rows(client, review_case)
    response = state(client, asset, version, "READY_FOR_REVIEW")
    assert response.json()["error"]["code"] == "REVIEW_ADMISSION_INVALID"


@pytest.mark.parametrize("conclusion", ["PASS", "REVISION_REQUIRED", "HUMAN_REVIEW"])
def test_necessary_human_acceptance_cannot_be_skipped(client, review_case, conclusion):
    value = deepcopy(review_case)
    if conclusion != "PASS":
        with_issue(value)
    if conclusion == "HUMAN_REVIEW":
        value.update(conclusion="HUMAN_REVIEW", suggested_action="HUMAN_REVIEW")
    record, _ = saved(client, value)
    admission_fixture(client, record, human=conclusion == "PASS")
    asset, version = target_rows(client, value)
    response = state(client, asset, version, "READY_FOR_REVIEW")
    expected = (
        "CONTINUITY_REVISION_REQUIRED"
        if conclusion == "REVISION_REQUIRED"
        else "HUMAN_DECISION_REQUIRED"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == expected


@pytest.mark.parametrize("decision,comment", [(None, ""), ("REJECT", "拒绝"), ("APPROVE", "  ")])
def test_missing_rejected_or_unreasoned_decisions_do_not_grant_acceptance(
    client, review_case, decision, comment
):
    record, _ = saved(client, review_case)
    request_id = human_request_fixture(client, record, decision=decision, comment=comment)
    admission_fixture(client, record, human=True, request_id=request_id)
    asset, version = target_rows(client, review_case)
    assert (
        state(client, asset, version, "READY_FOR_REVIEW").json()["error"]["code"]
        == "HUMAN_DECISION_REQUIRED"
    )


def test_exact_human_exception_preserves_original_review_and_never_self_selects(
    client, review_case
):
    value = with_issue(deepcopy(review_case), "HIGH")
    value.update(conclusion="HUMAN_REVIEW", suggested_action="HUMAN_REVIEW")
    record, _ = saved(client, value)
    request_id = human_request_fixture(client, record)
    admission_fixture(client, record, request_id=request_id)
    version = approve(client, value)
    asset, _ = target_rows(client, value)
    assert asset["current_version_id"] is None
    assert choose(client, asset, version).status_code == 200
    with client.app.state.session_factory() as db:
        assert require_shot_review(db, db.get(AssetVersion, UUID(version["id"]))) == record
        assert record.review.conclusion == "HUMAN_REVIEW"


@pytest.mark.parametrize(
    "changes",
    [
        {"object_id": str(uuid4())},
        {"input_sha256": "b" * 64},
        {"kind": "REFERENCE"},
        {"content_sha256": "b" * 64},
    ],
)
def test_human_decision_must_bind_this_review_not_just_the_image(client, review_case, changes):
    record, _ = saved(client, review_case)
    request_id = human_request_fixture(client, record, changes=changes)
    admission_fixture(client, record, human=True, request_id=request_id)
    asset, version = target_rows(client, review_case)
    response = state(client, asset, version, "READY_FOR_REVIEW")
    assert response.json()["error"]["code"] == "HUMAN_DECISION_REQUIRED"


def test_rerun_invalidates_old_pass_and_human_acceptance_without_rewriting_history(
    client, review_case
):
    record, _ = saved(client, review_case)
    request_id = human_request_fixture(client, record)
    admission_fixture(client, record, human=True, request_id=request_id)
    version = approve(client, review_case)
    value = with_issue(deepcopy(review_case))
    value.update(
        result_id=str(uuid4()),
        rerun={
            "previous_review_id": str(record.review.result_id),
            "previous_sha256": digest(record.model_dump(mode="json")),
            "reason": "发现新的服装矛盾，复核原图。",
        },
    )
    latest, _ = saved(client, value)
    asset, _ = target_rows(client, value)
    assert choose(client, asset, version).json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    admission_fixture(client, latest)
    assert choose(client, asset, version).json()["error"]["code"] == "CONTINUITY_REVISION_REQUIRED"
    with client.app.state.session_factory() as db:
        assert (
            require_review_record(
                db, record.review.project_id, record.review.result_id, "service-owner"
            )
            == record
        )


@pytest.mark.parametrize("kind", ["SHOT_IMAGE", "IMAGE"])
def test_manual_shot_upload_and_restored_copy_cannot_launder_review(client, review_case, kind):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    asset, _ = target_rows(client, review_case)
    if kind == "IMAGE":
        from arclight.persistence.models import VisualAsset

        with client.app.state.session_factory.begin() as db:
            image_asset = VisualAsset(
                project_id=record.review.project_id,
                asset_type=kind,
                name="通用图片",
                metadata_json={},
            )
            db.add(image_asset)
            db.flush()
            image_asset_id = image_asset.id
        asset = client.get(
            f"/api/v1/projects/{record.review.project_id}/assets/{image_asset_id}"
        ).json()
    manual = save(client, asset)
    response = state(client, asset, manual, "READY_FOR_REVIEW")
    assert response.json()["error"]["code"] == "MEDIA_CHECK_REQUIRED"
    copy = restore(client, current(client, asset), manual).json()
    assert copy["id"] != manual["id"] and copy["status"] == "DRAFT"
    assert (
        state(client, asset, copy, "READY_FOR_REVIEW").json()["error"]["code"]
        == "CONTINUITY_REVIEW_REQUIRED"
    )
    assert current(client, asset)["current_version_id"] is None


def retarget(client, value, candidate):
    result = deepcopy(value)
    result["result_id"] = str(uuid4())
    result["target"].update(
        candidate_id=candidate["id"],
        asset_id=candidate["version"]["asset_id"],
        asset_version_id=candidate["asset_version_id"],
        input_manifest_id=candidate["input_manifest_id"],
        media_check_sha256=digest(candidate["media_check"]),
    )
    result["references"][0].update(
        resource_id=candidate["id"], content_sha256=candidate["version"]["checksum"]
    )
    return result


def test_batch_siblings_and_duplicate_imports_do_not_share_review(client, episode, review_case):
    job, attempt = generate(client, episode["project_id"], count=2)
    assert ingest(client, attempt) == 2
    rows = candidates(client, episode["project_id"], job)
    value = retarget(client, review_case, rows[0])
    record, _ = saved(client, value)
    admission_fixture(client, record)
    approved = approve(client, value)
    assert ingest(client, attempt) == 0
    other = retarget(client, value, rows[1])
    asset, version = target_rows(client, other)
    assert choose(client, asset, version).json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    asset, _ = target_rows(client, value)
    assert choose(client, asset, approved).status_code == 200
    assert len(candidates(client, episode["project_id"], job)) == 2


def test_actual_failed_attempt_retry_creates_unreviewed_candidate(client, episode, review_case):
    old, _ = saved(client, review_case)
    admission_fixture(client, old)
    approve(client, review_case)
    asset, _ = target_rows(client, review_case)
    body = payload(asset)
    body["spec"]["output_spec"] = {"width": 8, "height": 12, "format": "png"}
    job = capture_ok(client, episode["project_id"], body)
    failed = Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED"))
    assert deliver(client, job, failed) == "FAILED"
    root = f"/api/v1/projects/{episode['project_id']}/generation-jobs/{job['id']}"
    current_job = client.get(root).json()
    response = client.post(root + "/retry", json={"expected_revision": current_job["revision"]})
    assert response.status_code == 200, response.text
    retry_due(client, job)
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(
                GenerationAttempt.job_id == UUID(job["id"]), GenerationAttempt.attempt_no == 2
            )
        )
    assert ingest(client, attempt) == 1
    row = candidates(client, episode["project_id"], job)[0]
    assert row["asset_version_id"] != review_case["target"]["asset_version_id"]
    assert (
        state(client, current(client, asset), row["version"], "READY_FOR_REVIEW").json()["error"][
            "code"
        ]
        == "CONTINUITY_REVIEW_REQUIRED"
    )


def test_existing_selected_pointer_stays_readable_but_rerun_blocks_new_lock(client, review_case):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    version = approve(client, review_case)
    asset, _ = target_rows(client, review_case)
    selected = choose(client, asset, version).json()
    changed = deepcopy(review_case)
    changed.update(
        result_id=str(uuid4()),
        rerun={
            "previous_review_id": str(record.review.result_id),
            "previous_sha256": digest(record.model_dump(mode="json")),
            "reason": "新证据重核。",
        },
    )
    saved(client, changed)
    before = effects(client)
    response = client.post(
        versions(asset) + f"/{version['id']}/lock",
        json={
            "expected_revision": selected["revision"],
            "expected_lock_version": selected["lock_version"],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    assert current(client, asset) == selected and effects(client) == before


def test_review_target_cannot_be_changed_to_cherry_pick_a_pass(client, review_case):
    original, _ = saved(client, review_case)
    admission_fixture(client, original)
    approve(client, review_case)
    with client.app.state.session_factory() as db:
        shot = db.scalar(
            select(Shot).where(
                Shot.storyboard_id == UUID(review_case["target"]["storyboard_id"]),
                Shot.id != UUID(review_case["target"]["shot_id"]),
            )
        )
        from arclight.application.continuity_reviews import shot_sha256

        value = deepcopy(review_case)
        value["target"]["shot_id"] = str(shot.id)
        value["references"][1].update(resource_id=str(shot.id), content_sha256=shot_sha256(shot))
        value["result_id"] = str(uuid4())
    saved(client, value)
    asset, version = target_rows(client, value)
    assert choose(client, asset, version).json()["error"]["code"] == "REVIEW_TARGET_AMBIGUOUS"


def test_authoritative_scope_is_revalidated_after_storyboard_withdrawal(client, review_case):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    version = approve(client, review_case)
    with client.app.state.session_factory.begin() as db:
        board = db.get(Storyboard, record.review.target.storyboard_id)
        board.status = "REJECTED"
    asset, _ = target_rows(client, review_case)
    response = choose(client, asset, version)
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "REVIEW_TARGET_INVALID"
    )


def test_s3_upload_replacement_cannot_inherit_original_admission(
    client, transfer_setup, review_case
):
    _settings, root = transfer_setup
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    asset, _ = target_rows(client, review_case)
    upload = ticket(client, root)
    file = complete(client, root, upload).json()
    endpoint = root + f"/generation-candidates/{review_case['target']['candidate_id']}/replacements"
    body = {
        "file_id": file["id"],
        "creation_key": str(uuid4()),
        "expected_revision": asset["revision"],
    }
    response = client.post(endpoint, json=body)
    assert response.status_code == 201, response.text
    replacement = response.json()
    assert replacement["media_check"]["status"] == "PASSED"
    assert replacement["asset_version_id"] != review_case["target"]["asset_version_id"]
    assert client.post(endpoint, json=body).json() == replacement
    response = state(client, current(client, asset), replacement["version"], "READY_FOR_REVIEW")
    assert response.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    with client.app.state.session_factory() as db:
        assert (
            require_review_record(
                db, record.review.project_id, record.review.result_id, "service-owner"
            )
            == record
        )


@pytest.mark.parametrize("changed", ["none", "intent", "episode"])
def test_storyboard_selected_image_preserves_reviewed_episode_and_intent(
    client, review_case, changed
):
    from arclight.agents.contracts import StoryboardContent
    from arclight.application.content_references import validate_storyboard_assets

    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    approve(client, review_case)
    with client.app.state.session_factory() as db:
        board = db.get(Storyboard, record.review.target.storyboard_id)
        content = deepcopy(board.content_json)
    content["shots"][0]["selected_visual_asset"] = {
        "asset_id": str(record.review.target.asset_id),
        "asset_version_id": str(record.review.target.asset_version_id),
    }
    if changed == "intent":
        content["shots"][0]["visual_description"] = "改为完全不同的动作和场景。"
    episode_id = uuid4() if changed == "episode" else record.review.episode_id
    with client.app.state.session_factory() as db:
        parsed = StoryboardContent.model_validate(content)
        if changed == "none":
            validate_storyboard_assets(db, record.review.project_id, parsed, episode_id=episode_id)
            assert not db.new and not db.dirty
        else:
            with pytest.raises(DomainError) as error:
                validate_storyboard_assets(
                    db, record.review.project_id, parsed, episode_id=episode_id
                )
            assert error.value.code == "REVIEW_TARGET_INVALID"


@pytest.mark.parametrize("changed_intent", [False, True])
def test_director_check_preserves_reviewed_second_shot_identity(
    client, review_case, settings, monkeypatch, changed_intent
):
    from types import SimpleNamespace

    from arclight.agents.contracts import ScriptContent
    from arclight.application import director_validation
    from arclight.application.continuity_reviews import shot_sha256
    from arclight.domain.director_revision import DirectorWorkingStoryboard
    from arclight.persistence.models import Script, WorkflowRun

    value = deepcopy(review_case)
    with client.app.state.session_factory() as db:
        board = db.get(Storyboard, UUID(value["target"]["storyboard_id"]))
        second = db.scalar(select(Shot).where(Shot.storyboard_id == board.id, Shot.shot_no == 2))
        value["target"]["shot_id"] = str(second.id)
        value["references"][1].update(
            resource_id=str(second.id), content_sha256=shot_sha256(second)
        )
        source = db.get(Script, board.script_version_id)
        script = ScriptContent.model_validate(source.content_json)
        content = deepcopy(board.content_json)
    record, _ = saved(client, value)
    admission_fixture(client, record)
    approve(client, value)
    content["shots"][1]["selected_visual_asset"] = {
        "asset_id": str(record.review.target.asset_id),
        "asset_version_id": str(record.review.target.asset_version_id),
    }
    if changed_intent:
        content["shots"][1]["visual_description"] = "改为未经审查的新动作。"
    work = DirectorWorkingStoryboard.model_validate(
        {
            "scenes": [
                {
                    "scene_id": scene.scene_id,
                    "scene_no": scene.scene_no,
                    "shots": [
                        shot for shot in content["shots"] if shot["scene_id"] == scene.scene_id
                    ],
                }
                for scene in script.scenes
            ]
        }
    )
    # Isolate this rule check from director planning; media admission remains real.
    monkeypatch.setattr(director_validation, "revision_input", lambda *args: script)
    plan = SimpleNamespace(
        scenes=[
            SimpleNamespace(
                scene_id=scene.scene_id,
                estimated_duration_ms=sum(shot.duration_ms for shot in scene.shots),
            )
            for scene in work.scenes
        ]
    )
    run = WorkflowRun(
        episode_id=record.review.episode_id,
        input_json={"target_duration_ms": sum(shot["duration_ms"] for shot in content["shots"])},
    )
    with client.app.state.session_factory() as db:
        result = director_validation.check_work(db, run, work, plan, settings, "a" * 64)
    assert result.valid is (not changed_intent), result.model_dump()
    assert [issue.code for issue in result.issues] == (
        ["REVIEW_TARGET_INVALID"] if changed_intent else []
    )


def test_storyboard_api_rejects_previously_approved_but_unreviewed_shot(client, review_case):
    target = review_case["target"]
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(target["asset_version_id"]))
        version.status = "APPROVED"  # Explicit historical approval is not continuity evidence.
        board = db.get(Storyboard, UUID(target["storyboard_id"]))
        content = deepcopy(board.content_json)
        script_id = str(board.script_version_id)
    content["shots"][0]["selected_visual_asset"] = {
        "asset_id": target["asset_id"],
        "asset_version_id": target["asset_version_id"],
    }
    before = effects(client)
    endpoint = f"/api/v1/episodes/{review_case['episode_id']}/storyboards"
    response = client.post(
        endpoint,
        json={
            "expected_version_no": 1,
            "script_version_id": script_id,
            "content": content,
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    assert effects(client) == before
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1


def test_valid_reviewed_selection_uses_existing_lock_and_unlock_protocol(client, review_case):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    version = approve(client, review_case)
    asset, _ = target_rows(client, review_case)
    selected = choose(client, asset, version).json()
    locked = client.post(
        versions(asset) + f"/{version['id']}/lock",
        json={
            "expected_revision": selected["revision"],
            "expected_lock_version": selected["lock_version"],
        },
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["status"] == "LOCKED"
    assert choose(client, locked.json(), version).json()["error"]["code"] == "ASSET_READ_ONLY"
    unlocked = client.post(
        versions(asset) + f"/{version['id']}/unlock",
        json={
            "expected_revision": locked.json()["revision"],
            "expected_lock_version": locked.json()["lock_version"],
        },
    )
    assert unlocked.status_code == 200, unlocked.text
    assert unlocked.json()["current_version_id"] == version["id"]
    with client.app.state.session_factory() as db:
        assert require_shot_review(db, db.get(AssetVersion, UUID(version["id"]))) == record


def test_postgres_selection_reloads_review_committed_while_waiting_for_project_gate(
    client, review_case
):
    from concurrent.futures import TimeoutError

    from arclight.application.commands import require_project
    from arclight.application.continuity_review_store import save_review_contract
    from arclight.domain.continuity_reviews import ContinuityReview

    require_postgres(client.app.state.settings)
    original, _ = saved(client, review_case)
    admission_fixture(client, original)
    version = approve(client, review_case)
    asset, _ = target_rows(client, review_case)
    value = with_issue(deepcopy(review_case))
    value.update(
        result_id=str(uuid4()),
        rerun={
            "previous_review_id": str(original.review.result_id),
            "previous_sha256": digest(original.model_dump(mode="json")),
            "reason": "门禁等待期间登记新的矛盾。",
        },
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        with client.app.state.session_factory.begin() as db:
            require_project(db, original.review.project_id, "service-owner", write=True)
            save_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
            future = pool.submit(choose, client, asset, version)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
        response = future.result(timeout=30)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    assert current(client, asset)["current_version_id"] is None


def test_decided_human_review_does_not_reenter_original_run_locks(client, review_case, monkeypatch):
    from arclight.application import human_reviews
    from arclight.persistence.models import WorkflowRun

    record, _ = saved(client, review_case)
    with client.app.state.session_factory.begin() as db:
        run = WorkflowRun(
            episode_id=record.review.episode_id,
            trace_id=uuid4().hex,
            idempotency_key=uuid4().hex,
            request_fingerprint=digest({}),
            input_json={},
        )
        db.add(run)
        db.flush()
        run_id = run.id
    request_id = human_request_fixture(
        client, record, changes={"run_id": str(run_id), "thread_id": str(run_id)}
    )
    admission_fixture(client, record, human=True, request_id=request_id)
    version = approve(client, review_case)

    def runtime_lock_is_not_selection_authority(*_args, **_kwargs):
        raise AssertionError("Selecting an already decided image must not lock its original run")

    monkeypatch.setattr(human_reviews, "require_run", runtime_lock_is_not_selection_authority)
    asset, _ = target_rows(client, review_case)
    response = choose(client, asset, version)
    assert response.status_code == 200, response.text


def test_postgres_concurrent_selection_preserves_existing_cas(client, review_case):
    require_postgres(client.app.state.settings)
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    version = approve(client, review_case)
    asset, _ = target_rows(client, review_case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: choose(client, asset, version), range(2)))
    assert sorted(item.status_code for item in responses) == [200, 409]
    assert current(client, asset)["current_version_id"] == version["id"]
