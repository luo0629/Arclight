"""QA-08 read model uses real stored contracts and the existing write gates."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_asset_selection import approved, choose, current, restore
from test_asset_versions import state
from test_assets import create
from test_collaboration import register
from test_continuity_review_contracts import with_issue
from test_continuity_reviews import review_case as review_case
from test_continuity_reviews import saved
from test_continuity_selection import admission_fixture, approve, effects, target_rows
from test_file_transfers import transfer_setup as transfer_setup
from test_generation_candidates import candidates, generate, ingest
from test_generation_delivery import deliver
from test_generation_jobs import capture_ok, payload
from test_upload_completion import complete, ticket

from arclight.domain.versions import digest
from arclight.persistence.models import (
    AssetVersion,
    GenerationAttempt,
    Project,
    ProjectMember,
    VisualAsset,
)
from arclight.providers.media_submission import MockMediaSubmissionProvider


def comparison_path(case):
    target = case["target"]
    return (
        f"/api/v1/projects/{case['project_id']}/assets/{target['asset_id']}/versions/"
        f"{target['asset_version_id']}/continuity"
    )


def comparison(client, case, **params):
    response = client.get(comparison_path(case), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_unreviewed_candidate_is_readable_without_fabricating_evidence_or_writing(
    client, review_case
):
    before = effects(client)
    result = comparison(client, review_case)
    assert result["reviews"] == [] and result["latest_review_id"] is None
    assert result["candidate"]["id"] == review_case["target"]["candidate_id"]
    assert result["candidate"]["media_check"]["status"] == "PASSED"
    assert result["actions"]["blocker"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    assert not any(result["actions"][key] for key in ("can_submit", "can_approve", "can_select"))
    assert len(result["images"]) == 1 and result["images"][0]["round"] == 0
    assert client.get("/api/v1" + result["image_path"]).status_code == 200
    assert effects(client) == before
    asset, version = target_rows(client, review_case)
    assert choose(client, asset, version).json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"


def test_exact_report_paths_and_lifecycle_actions_never_implicitly_approve_or_select(
    client, review_case
):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    before = effects(client)
    result = comparison(client, review_case)
    assert result["reviews"][0]["record"] == record.model_dump(mode="json")
    assert result["latest_review_id"] == str(record.review.result_id)
    assert result["next_offset"] is None and result["next_human_offset"] is None
    assert result["actions"]["blocker"] is None
    assert result["actions"]["can_submit"] and not result["actions"]["can_select"]
    assert result["asset"]["current_version_id"] is None
    assert result["version"]["status"] == "DRAFT"
    refs = result["reviews"][0]["references"]
    assert [item["reference"] for item in refs] == [
        item.model_dump(mode="json") for item in record.review.references
    ]
    assert refs[0]["image_path"] == result["image_path"]
    assert effects(client) == before
    asset, version = target_rows(client, review_case)
    ready = state(client, asset, version, "READY_FOR_REVIEW")
    assert ready.status_code == 200
    result = comparison(client, review_case)
    assert result["actions"]["can_approve"] and result["actions"]["can_reject"]
    approved = state(client, asset, ready.json(), "APPROVED")
    assert approved.status_code == 200
    result = comparison(client, review_case)
    assert result["actions"]["can_select"] and result["asset"]["current_version_id"] is None
    assert choose(client, result["asset"], result["version"]).status_code == 200
    stale_cas = choose(client, result["asset"], result["version"])
    assert stale_cas.status_code == 409 and stale_cas.json()["error"]["code"] == "VERSION_CONFLICT"


@pytest.mark.parametrize("conclusion", ["REVISION_REQUIRED", "HUMAN_REVIEW"])
def test_issues_evidence_and_missing_human_remain_visible_and_blocked(
    client, review_case, conclusion
):
    value = with_issue(deepcopy(review_case))
    if conclusion == "HUMAN_REVIEW":
        value.update(conclusion=conclusion, suggested_action="HUMAN_REVIEW")
    record, _ = saved(client, value)
    admission_fixture(client, record, human=conclusion == "HUMAN_REVIEW")
    result = comparison(client, value)
    review = result["reviews"][0]["record"]["review"]
    assert review["issues"] == value["issues"] and review["evidence"] == value["evidence"]
    assert not result["actions"]["can_submit"] and not result["actions"]["can_select"]
    assert result["human_reviews"] == []
    expected = (
        "CONTINUITY_REVISION_REQUIRED"
        if conclusion == "REVISION_REQUIRED"
        else "HUMAN_DECISION_REQUIRED"
    )
    assert result["actions"]["blocker"]["code"] == expected


def test_history_paging_cannot_replace_the_latest_gate_with_an_older_pass(client, review_case):
    original, _ = saved(client, review_case)
    admission_fixture(client, original)
    value = with_issue(deepcopy(review_case))
    value["result_id"] = str(uuid4())
    value["rerun"] = {
        "previous_review_id": str(original.review.result_id),
        "previous_sha256": digest(original.model_dump(mode="json")),
        "reason": "重核道具位置",
    }
    latest, _ = saved(client, value)
    admission_fixture(client, latest)
    first = comparison(client, value, limit=1)
    old = comparison(client, value, limit=1, offset=first["next_offset"])
    empty = comparison(client, value, offset=50)
    assert first["next_offset"] == 1 and old["next_offset"] is None
    assert first["reviews"][0]["record"]["sequence"] == 2
    assert old["reviews"][0]["record"] == original.model_dump(mode="json")
    assert empty["reviews"] == []
    for result in (first, old, empty):
        assert result["latest_review_id"] == str(latest.review.result_id)
        assert result["actions"]["blocker"]["code"] == "CONTINUITY_REVISION_REQUIRED"
        assert not result["actions"]["can_select"]


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_current_membership_is_required_for_read_and_owner_for_shot_selection(
    client, review_case, role
):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    version = approve(client, review_case)
    asset, _ = target_rows(client, review_case)
    session = register(client, f"qa08-{role}@example.com")
    identity = UUID(session["user"]["id"])
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(review_case["project_id"]), user_id=identity, role=role
                )
            )
    response = client.get(comparison_path(review_case))
    assert response.status_code == (404 if role == "outsider" else 200)
    if role != "outsider":
        actions = response.json()["actions"]
        assert actions["writable"] == (role == "editor") and not actions["owner"]
        assert not actions["can_select"] and not actions["can_approve"]
    assert choose(client, asset, version).status_code == (404 if role == "outsider" else 403)
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.delete(db.scalar(select(ProjectMember).where(ProjectMember.user_id == identity)))
        assert client.get(comparison_path(review_case)).status_code == 404
        assert (
            client.get(
                comparison_path(review_case).removesuffix("/continuity") + "/image"
            ).status_code
            == 404
        )
    client.headers["Authorization"] = "Bearer test-token"
    assert current(client, asset)["current_version_id"] is None


@pytest.mark.parametrize("readonly", ["project", "asset", "version"])
def test_archival_lock_and_stale_keep_history_readable_but_disable_writes(
    client, review_case, readonly
):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    approve(client, review_case)
    with client.app.state.session_factory.begin() as db:
        if readonly == "project":
            db.get(Project, UUID(review_case["project_id"])).archived = True
        elif readonly == "asset":
            db.get(VisualAsset, UUID(review_case["target"]["asset_id"])).status = "LOCKED"
        else:
            db.get(AssetVersion, UUID(review_case["target"]["asset_version_id"])).stale = True
    result = comparison(client, review_case)
    assert result["reviews"][0]["record"] == record.model_dump(mode="json")
    assert not any(
        result["actions"][key] for key in ("can_submit", "can_approve", "can_reject", "can_select")
    )


def test_rejected_version_can_be_compared_but_never_selected(client, review_case):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    asset, version = target_rows(client, review_case)
    ready = state(client, asset, version, "READY_FOR_REVIEW").json()
    rejected = state(client, asset, ready, "REJECTED")
    assert rejected.status_code == 200
    result = comparison(client, review_case)
    assert result["version"]["status"] == "REJECTED" and not result["actions"]["can_select"]
    assert choose(client, result["asset"], result["version"]).status_code == 409


def test_restored_image_has_its_own_missing_review_gate_and_original_is_preserved(
    client, review_case
):
    # Exercise the existing explicit-input restore contract with a real reference.
    project_id = review_case["project_id"]
    reference_asset = create(client, project_id)
    reference = approved(client, reference_asset)
    job = capture_ok(
        client,
        project_id,
        payload(
            inputs=[
                {
                    "source_asset_version_id": reference["id"],
                    "dependency_type": "REFERENCE_DEPENDENCY",
                }
            ]
        ),
    )
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 1
    candidate = candidates(client, project_id, job)[0]
    case = deepcopy(review_case)
    case["result_id"] = str(uuid4())
    case["target"].update(
        candidate_id=candidate["id"],
        asset_id=candidate["version"]["asset_id"],
        asset_version_id=candidate["asset_version_id"],
        input_manifest_id=candidate["input_manifest_id"],
        media_check_sha256=digest(candidate["media_check"]),
    )
    case["references"][0].update(
        resource_id=candidate["id"], content_sha256=candidate["version"]["checksum"]
    )
    case["references"].append(
        {
            "reference_id": str(uuid4()),
            "role": "REFERENCE",
            "resource_id": reference["id"],
            "asset_id": reference_asset["id"],
            "asset_type": reference_asset["asset_type"],
            "content_sha256": reference["checksum"],
        }
    )
    record, _ = saved(client, case)
    admission_fixture(client, record)
    approve(client, case)
    asset, version = target_rows(client, case)
    assert choose(client, asset, version).status_code == 200
    asset = current(client, asset)
    root = comparison_path(case).removesuffix("/continuity")
    manifest = client.post(
        root + "/rebuild-manifest",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": version["revision"],
            "replacements": {},
        },
    )
    assert manifest.status_code == 201, manifest.text
    response = restore(client, asset, version, dependency_manifest_id=manifest.json()["id"])
    assert response.status_code == 201, response.text
    restored = response.json()
    restored_case = {
        "project_id": project_id,
        "target": {"asset_id": asset["id"], "asset_version_id": restored["id"]},
    }
    result = comparison(client, restored_case)
    assert result["candidate"] is None and result["reviews"] == []
    assert result["actions"]["blocker"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    assert result["version"]["restored_from_version_id"] == version["id"]
    assert {item["version"]["id"] for item in result["images"]} == {version["id"], restored["id"]}
    assert not result["actions"]["can_select"] and not result["actions"]["can_submit"]
    assert comparison(client, case)["asset"]["current_version_id"] == version["id"]


def test_legacy_image_without_candidate_explains_missing_media_check(client, review_case):
    with client.app.state.session_factory.begin() as db:
        legacy = AssetVersion(
            asset_id=UUID(review_case["target"]["asset_id"]),
            version_no=2,
            object_key="legacy/unavailable",
            checksum="a" * 64,
            created_by="service-owner",
        )
        db.add(legacy)
        db.flush()
        version_id = str(legacy.id)
    case = {
        "project_id": review_case["project_id"],
        "target": {"asset_id": review_case["target"]["asset_id"], "asset_version_id": version_id},
    }
    result = comparison(client, case)
    assert result["candidate"] is None and result["reviews"] == []
    assert result["actions"]["blocker"]["code"] == "MEDIA_CHECK_REQUIRED"
    assert not result["actions"]["can_select"]


@pytest.mark.parametrize(
    "fault", ["project", "asset", "version", "negative_offset", "large_limit", "negative_human"]
)
def test_comparison_scopes_and_pagination_are_bounded(client, review_case, fault):
    path = comparison_path(review_case)
    params = {}
    if fault in {"project", "asset", "version"}:
        original = (
            review_case["project_id"]
            if fault == "project"
            else review_case["target"][f"asset{'_version' if fault == 'version' else ''}_id"]
        )
        path = path.replace(original, str(uuid4()))
    else:
        params = {
            "negative_offset": {"offset": -1},
            "large_limit": {"limit": 101},
            "negative_human": {"human_offset": -1},
        }[fault]
    assert client.get(path, params=params).status_code == (404 if not params else 422)


def test_batch_siblings_are_comparable_with_independent_gates_and_no_unrelated_images(
    client, review_case
):
    project_id = review_case["project_id"]
    job, attempt = generate(client, project_id, count=2)
    assert ingest(client, attempt) == 2
    rows = candidates(client, project_id, job)
    assert len(rows) == 2
    before = effects(client)
    for row in rows:
        result = comparison(
            client,
            {
                "project_id": project_id,
                "target": {**row, "asset_id": row["version"]["asset_id"]},
            },
        )
        assert {item["candidate_id"] for item in result["images"]} == {item["id"] for item in rows}
        assert result["candidate"]["id"] == row["id"]
        assert result["reviews"] == [] and result["latest_review_id"] is None
        assert result["actions"]["blocker"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
        assert not result["actions"]["can_select"]
        assert not result["images_truncated"]
        for image in result["images"]:
            response = client.get("/api/v1" + image["image_path"])
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"
    assert effects(client) == before


def test_uploaded_replacement_compares_to_original_without_inheriting_its_gate(
    client, transfer_setup
):
    _, root = transfer_setup
    project_id = root.rsplit("/", 1)[1]
    job, attempt = generate(client, project_id)
    assert ingest(client, attempt) == 1
    original = candidates(client, project_id, job)[0]
    asset = {"id": original["version"]["asset_id"], "project_id": project_id}
    upload = ticket(client, root)
    completed = complete(client, root, upload)
    assert completed.status_code == 200, completed.text
    response = client.post(
        root + f"/generation-candidates/{original['id']}/replacements",
        json={
            "file_id": completed.json()["id"],
            "creation_key": str(uuid4()),
            "expected_revision": current(client, asset)["revision"],
        },
    )
    assert response.status_code == 201, response.text
    replacement = response.json()
    assert replacement["replaces_candidate_id"] == original["id"]
    before = effects(client)
    for candidate in (original, replacement):
        result = comparison(
            client,
            {
                "project_id": project_id,
                "target": {**candidate, "asset_id": candidate["version"]["asset_id"]},
            },
        )
        assert {image["candidate_id"] for image in result["images"]} == {
            original["id"],
            replacement["id"],
        }
        assert result["candidate"]["id"] == candidate["id"]
        assert result["reviews"] == [] and result["latest_review_id"] is None
        assert result["actions"]["blocker"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
        assert not result["actions"]["can_select"]
        assert result["asset"]["current_version_id"] is None
    assert effects(client) == before
