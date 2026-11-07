from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_collaboration import create_project, register
from test_concurrency import require_postgres
from test_contexts import approved_board
from test_continuity_review_contracts import review_payload, with_issue
from test_generation_candidates import candidates, generate, ingest
from test_text_contract_v2 import make_asset, save_script

from arclight.application.continuity_review_store import (
    REVIEW_RESOURCE,
    require_review_record,
    save_review_contract,
)
from arclight.application.continuity_reviews import shot_sha256, validate_review_contract
from arclight.application.generation_candidates import require_candidate_review
from arclight.application.production_versions import checked_snapshot, find_binding
from arclight.application.production_versions import register as register_version
from arclight.core.errors import DomainError
from arclight.domain.continuity_reviews import (
    ContinuityReview,
    ReviewContractRecord,
    review_schema_registration,
)
from arclight.domain.versions import VersionRegistration, digest
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    Episode,
    Project,
    ProjectMember,
    Shot,
    VersionBinding,
    VisualAsset,
)


@pytest.fixture
def review_case(client, episode):
    script = save_script(client, episode)
    board, shots = approved_board(client, episode, script)
    job, attempt = generate(client, episode["project_id"])
    assert ingest(client, attempt) == 1
    candidate = candidates(client, episode["project_id"], job)[0]
    value = review_payload()
    value.update(project_id=episode["project_id"], episode_id=episode["id"])
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(candidate["asset_version_id"]))
        value["target"] = {
            "candidate_id": candidate["id"],
            "asset_id": str(version.asset_id),
            "asset_version_id": str(version.id),
            "input_manifest_id": candidate["input_manifest_id"],
            "media_check_sha256": digest(candidate["media_check"]),
            "storyboard_id": board["id"],
            "shot_id": shots[0]["id"],
        }
        for ref, identity, checksum in zip(
            value["references"],
            [candidate["id"], shots[0]["id"], board["id"]],
            [
                version.checksum,
                shot_sha256(db.get(Shot, UUID(shots[0]["id"]))),
                digest(board["content_json"]),
            ],
            strict=True,
        ):
            ref.update(resource_id=identity, content_sha256=checksum)
        value["versions"] = []
        for kind in ("AGENT", "PROMPT", "SCHEMA", "MODEL"):
            specification = (
                review_schema_registration()
                if kind == "SCHEMA"
                else VersionRegistration(
                    family="REVIEW",
                    kind=kind,
                    name="fixture.review." + kind,
                    version="fixture/1",
                    definition={"origin": "CONTRACT_FIXTURE", "component": kind},
                )
            )
            row = register_version(db, UUID(episode["project_id"]), specification)
            value["versions"].append(
                {
                    "id": str(row.id),
                    "family": row.family,
                    "kind": row.kind,
                    "name": row.name,
                    "version": row.version,
                    "digest": row.digest,
                }
            )
    return value


def saved(client, value, actor="service-owner"):
    with client.app.state.session_factory.begin() as db:
        return save_review_contract(db, ContinuityReview.model_validate(value), actor)


def test_actual_contract_validation_is_read_only_and_does_not_promote_candidate(
    client, review_case
):
    review = ContinuityReview.model_validate(review_case)
    with client.app.state.session_factory() as db:
        counts = {
            table: db.scalar(select(func.count()).select_from(table))
            for table in (VersionBinding, AuditEvent)
        }
        validate_review_contract(db, review, "service-owner")
        assert counts == {
            table: db.scalar(select(func.count()).select_from(table)) for table in counts
        }
        assert not db.new and not db.dirty
    record, created = saved(client, review_case)
    assert created and record.state == "CONTRACT_VALIDATED" and record.sequence == 1
    assert record.recorded_at.tzinfo == UTC
    assert record.result.requires_human_review is True
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, review.target.asset_version_id)
        asset = db.get(VisualAsset, version.asset_id)
        assert version.status == "DRAFT" and asset.current_version_id is None
        binding = find_binding(db, review.project_id, REVIEW_RESOURCE, review.result_id)
        assert checked_snapshot(db, binding)["review_contract"] == record.model_dump(mode="json")
    with client.app.state.session_factory() as db, pytest.raises(DomainError) as exc:
        require_candidate_review(db.get(AssetVersion, review.target.asset_version_id), db)
    assert exc.value.code == "CONTINUITY_REVIEW_REQUIRED"


def test_exact_reference_hash_identity_and_registered_version_mismatches_fail(client, review_case):
    cases = []
    for field in ("asset_id", "asset_version_id", "input_manifest_id"):
        value = deepcopy(review_case)
        value["target"][field] = str(uuid4())
        cases.append(value)
    value = deepcopy(review_case)
    value["target"]["media_check_sha256"] = "0" * 64
    cases.append(value)
    for index in range(3):
        value = deepcopy(review_case)
        value["references"][index]["content_sha256"] = "0" * 64
        cases.append(value)
    for field in ("id", "name", "version", "digest"):
        value = deepcopy(review_case)
        value["versions"][0][field] = str(uuid4()) if field == "id" else "0" * 64
        cases.append(value)
    with client.app.state.session_factory() as db:
        for value in cases:
            with pytest.raises(DomainError):
                validate_review_contract(
                    db, ContinuityReview.model_validate(value), "service-owner"
                )
        assert not db.new and not db.dirty


def test_schema_claim_must_match_the_actual_current_contract_definition(client, review_case):
    with client.app.state.session_factory.begin() as db:
        row = register_version(
            db,
            UUID(review_case["project_id"]),
            VersionRegistration(
                family="REVIEW",
                kind="SCHEMA",
                name="continuity.review",
                version="continuity-review/1",
                definition={"schema": {"type": "object"}, "authority": "advisory-only/1"},
            ),
        )
        value = deepcopy(review_case)
        value["versions"][2].update(id=str(row.id), digest=row.digest)
        with pytest.raises(DomainError) as exc:
            validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
        assert exc.value.code == "REVIEW_VERSION_INVALID"


def test_approved_asset_reference_uses_existing_identity_approval_and_stale_checks(
    client, review_case
):
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, review_case["project_id"], "CHARACTER")
    value = deepcopy(review_case)
    value["references"].append(
        {
            "reference_id": str(uuid4()),
            "role": "REFERENCE",
            "resource_id": reference["asset_version_id"],
            "asset_id": reference["asset_id"],
            "asset_type": "CHARACTER",
            "content_sha256": "a" * 64,
        }
    )
    with client.app.state.session_factory() as db:
        validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
        for field, replacement in (
            ("asset_id", str(uuid4())),
            ("asset_type", "PROP"),
            ("content_sha256", "0" * 64),
        ):
            invalid = deepcopy(value)
            invalid["references"][-1][field] = replacement
            with pytest.raises(DomainError):
                validate_review_contract(
                    db, ContinuityReview.model_validate(invalid), "service-owner"
                )
    for state_field, state_value in (("status", "DRAFT"), ("stale", True)):
        with client.app.state.session_factory.begin() as db:
            row = db.get(AssetVersion, UUID(reference["asset_version_id"]))
            row.status = "APPROVED"
            row.stale = False
            setattr(row, state_field, state_value)
        with client.app.state.session_factory() as db, pytest.raises(DomainError):
            validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")


def test_foreign_project_candidate_assets_episode_and_reviewer_are_not_accepted(
    client, review_case
):
    foreign = create_project(client)
    with client.app.state.session_factory.begin() as db:
        ref = make_asset(db, foreign["id"], "IMAGE")
    value = deepcopy(review_case)
    value["references"].append(
        {
            "reference_id": str(uuid4()),
            "role": "REFERENCE",
            "resource_id": ref["asset_version_id"],
            "asset_id": ref["asset_id"],
            "asset_type": "IMAGE",
            "content_sha256": "a" * 64,
        }
    )
    with client.app.state.session_factory() as db:
        with pytest.raises(DomainError):
            validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
        value = deepcopy(review_case)
        value["project_id"] = foreign["id"]
        with pytest.raises(DomainError):
            validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
        with pytest.raises(DomainError):
            validate_review_contract(db, ContinuityReview.model_validate(review_case), str(uuid4()))


def test_adjacent_evidence_is_exact_and_preserved_without_implicit_current_versions(
    client, review_case
):
    with client.app.state.session_factory() as db:
        shots = list(
            db.scalars(
                select(Shot)
                .where(Shot.storyboard_id == UUID(review_case["target"]["storyboard_id"]))
                .order_by(Shot.shot_no)
            )
        )
        assert len(shots) >= 2
        value = deepcopy(review_case)
        value["references"].append(
            {
                "reference_id": str(uuid4()),
                "role": "ADJACENT_SHOT",
                "resource_id": str(shots[1].id),
                "content_sha256": shot_sha256(shots[1]),
            }
        )
        validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
        value["references"][-1].update(
            resource_id=str(shots[0].id), content_sha256=shot_sha256(shots[0])
        )
        with pytest.raises(DomainError):
            validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")


def test_rerun_appends_new_versions_reasons_and_audit_without_changing_original(
    client, review_case
):
    original, created = saved(client, review_case)
    assert created
    old = original.model_dump(mode="json")
    replay, created = saved(client, review_case)
    assert not created and replay == original
    value = with_issue(deepcopy(review_case))
    value["result_id"] = str(uuid4())
    value["rerun"] = {
        "previous_review_id": review_case["result_id"],
        "previous_sha256": digest(old),
        "reason": "使用明确的服装版本重核。",
    }
    with client.app.state.session_factory.begin() as db:
        row = register_version(
            db,
            UUID(value["project_id"]),
            VersionRegistration(
                family="REVIEW",
                kind="PROMPT",
                name="fixture.review.PROMPT",
                version="fixture/2",
                definition={"origin": "CONTRACT_FIXTURE", "instruction": "补充服装对照"},
            ),
        )
        value["versions"][1].update(id=str(row.id), version=row.version, digest=row.digest)
    second, created = saved(client, value)
    assert created and second.sequence == 2 and second.input_sha256 != original.input_sha256
    assert second.review.rerun.reason == value["rerun"]["reason"]
    with client.app.state.session_factory() as db:
        assert (
            require_review_record(
                db, UUID(review_case["project_id"]), UUID(review_case["result_id"]), "service-owner"
            ).model_dump(mode="json")
            == old
        )
        audit_rows = list(
            db.scalars(
                select(AuditEvent).where(AuditEvent.action == "continuity_review.contract_saved")
            )
        )
        assert len(audit_rows) == 2
        assert {row.details_json["sequence"] for row in audit_rows} == {1, 2}
        assert all("summary" not in row.details_json for row in audit_rows)
    with pytest.raises(DomainError):
        saved(client, dict(review_case, summary="更换相同ID的内容"))
    third = deepcopy(value)
    third["result_id"] = str(uuid4())
    with pytest.raises(DomainError) as exc:
        saved(client, third)
    assert exc.value.code == "REVIEW_RERUN_CONFLICT"


@pytest.mark.parametrize("fault", ["missing", "foreign_parent", "hash", "different_shot"])
def test_rerun_must_name_the_exact_latest_receipt(client, review_case, fault):
    original, _ = saved(client, review_case)
    value = deepcopy(review_case)
    value["result_id"] = str(uuid4())
    if fault != "missing":
        value["rerun"] = {
            "previous_review_id": review_case["result_id"],
            "previous_sha256": digest(original.model_dump(mode="json")),
            "reason": "独立重查",
        }
        if fault == "foreign_parent":
            value["rerun"]["previous_review_id"] = str(uuid4())
        elif fault == "hash":
            value["rerun"]["previous_sha256"] = "0" * 64
        else:
            value["target"]["storyboard_id"] = str(uuid4())
            value["references"][2]["resource_id"] = value["target"]["storyboard_id"]
    with pytest.raises(DomainError):
        saved(client, value)


def test_history_receipt_and_binding_cannot_be_overwritten_or_deleted(client, review_case):
    original, _ = saved(client, review_case)
    with client.app.state.session_factory() as db:
        binding_id = find_binding(
            db, UUID(review_case["project_id"]), REVIEW_RESOURCE, UUID(review_case["result_id"])
        ).id
    for sql in (
        "UPDATE version_bindings SET snapshot_json='{}' WHERE id=:identity",
        "DELETE FROM version_bindings WHERE id=:identity",
    ):
        with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
            db.execute(
                text(sql),
                {"identity": binding_id.hex if db.bind.dialect.name == "sqlite" else binding_id},
            )
    with client.app.state.session_factory() as db:
        assert (
            require_review_record(
                db, UUID(review_case["project_id"]), UUID(review_case["result_id"]), "service-owner"
            )
            == original
        )
    broken = original.model_dump(mode="json")
    broken["review"]["summary"] = "changed"
    with pytest.raises(ValidationError):
        ReviewContractRecord.model_validate(broken)


def test_rollback_never_publishes_an_audit_or_receipt(client, review_case):
    with pytest.raises(RuntimeError), client.app.state.session_factory.begin() as db:
        save_review_contract(db, ContinuityReview.model_validate(review_case), "service-owner")
        raise RuntimeError("simulate a caller transaction failure")
    with client.app.state.session_factory() as db:
        assert (
            find_binding(
                db, UUID(review_case["project_id"]), REVIEW_RESOURCE, UUID(review_case["result_id"])
            )
            is None
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "continuity_review.contract_saved")
            )
            == 0
        )
    assert saved(client, review_case)[1] is True


def test_viewer_can_read_receipts_but_cannot_write_and_revocation_is_rechecked(client, review_case):
    original, _ = saved(client, review_case)
    member = register(client, "review-viewer@example.com")
    actor = member["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(review_case["project_id"]), user_id=UUID(actor), role="viewer"
            )
        )
    with client.app.state.session_factory() as db:
        assert (
            require_review_record(
                db, UUID(review_case["project_id"]), UUID(review_case["result_id"]), actor
            )
            == original
        )
    with pytest.raises(DomainError) as exc:
        saved(client, review_case, actor)
    assert exc.value.code == "FORBIDDEN"
    with client.app.state.session_factory.begin() as db:
        db.delete(db.scalar(select(ProjectMember).where(ProjectMember.user_id == UUID(actor))))
    with client.app.state.session_factory() as db, pytest.raises(DomainError) as exc:
        require_review_record(
            db, UUID(review_case["project_id"]), UUID(review_case["result_id"]), actor
        )
    assert exc.value.code == "NOT_FOUND"


@pytest.mark.parametrize("resource", ["project", "episode", "candidate"])
def test_current_archival_and_stale_protection_keep_history_readable(client, review_case, resource):
    original, _ = saved(client, review_case)
    with client.app.state.session_factory.begin() as db:
        if resource == "project":
            db.get(Project, UUID(review_case["project_id"])).archived = True
        elif resource == "episode":
            db.get(Episode, UUID(review_case["episode_id"])).status = "ARCHIVED"
        else:
            db.get(AssetVersion, UUID(review_case["target"]["asset_version_id"])).stale = True
    with client.app.state.session_factory() as db:
        assert (
            require_review_record(
                db, UUID(review_case["project_id"]), UUID(review_case["result_id"]), "service-owner"
            )
            == original
        )
    changed = deepcopy(review_case)
    changed["result_id"] = str(uuid4())
    with pytest.raises(DomainError):
        saved(client, changed)


def test_legacy_candidate_evidence_is_not_fabricated_or_upgraded(client, review_case):
    value = deepcopy(review_case)
    value["target"]["candidate_id"] = str(uuid4())
    value["references"][0]["resource_id"] = value["target"]["candidate_id"]
    with client.app.state.session_factory() as db, pytest.raises(DomainError) as exc:
        validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
    assert exc.value.code == "NOT_FOUND"
    with client.app.state.session_factory() as db:
        assert (
            find_binding(db, UUID(value["project_id"]), REVIEW_RESOURCE, UUID(value["result_id"]))
            is None
        )


def test_postgres_duplicate_contract_saves_share_one_immutable_artifact_and_audit(
    client, review_case, settings
):
    require_postgres(settings)
    barrier = Barrier(2)
    review = ContinuityReview.model_validate(review_case)

    def execute():
        barrier.wait(timeout=15)
        with client.app.state.session_factory.begin() as db:
            record, created = save_review_contract(db, review, "service-owner")
            return record.model_dump(mode="json"), created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert sum(created for _, created in results) == 1
    assert results[0][0] == results[1][0]
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == REVIEW_RESOURCE)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "continuity_review.contract_saved")
            )
            == 1
        )


def test_historical_visual_bible_reference_is_exact_and_not_replaced_by_new_current(
    client, review_case
):
    from test_visual_bible import edit

    from arclight.application.art_proposals import visual_version_sha256
    from arclight.persistence.models import VisualBibleVersion

    first = edit(client, {"id": review_case["project_id"]}, palette="蓝灰")
    assert first.status_code == 201, first.text
    identity = UUID(first.json()["id"])
    with client.app.state.session_factory() as db:
        bible = db.get(VisualBibleVersion, identity)
        value = deepcopy(review_case)
        value["references"].append(
            {
                "reference_id": str(uuid4()),
                "role": "VISUAL_BIBLE",
                "resource_id": str(identity),
                "content_sha256": visual_version_sha256(bible),
            }
        )
        validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
    updated = edit(client, {"id": review_case["project_id"]}, expected=1, palette="暖灰")
    assert updated.status_code == 201, updated.text
    with client.app.state.session_factory() as db:
        validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
        value["references"][-1]["content_sha256"] = "0" * 64
        with pytest.raises(DomainError) as exc:
            validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
        assert exc.value.code == "REVIEW_REFERENCE_CHANGED"


def test_generated_reference_cannot_be_omitted_from_the_review_input(client, review_case):
    from test_asset_selection import approved
    from test_assets import create
    from test_generation_delivery import deliver
    from test_generation_jobs import capture_ok, payload

    from arclight.persistence.models import GenerationAttempt
    from arclight.providers.media_submission import MockMediaSubmissionProvider

    source = create(client, review_case["project_id"])
    reference = approved(client, source)
    job = capture_ok(
        client,
        review_case["project_id"],
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
    candidate = candidates(client, review_case["project_id"], job)[0]
    value = deepcopy(review_case)
    value["result_id"] = str(uuid4())
    value["target"].update(
        candidate_id=candidate["id"],
        asset_version_id=candidate["asset_version_id"],
        input_manifest_id=candidate["input_manifest_id"],
        media_check_sha256=digest(candidate["media_check"]),
    )
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(candidate["asset_version_id"]))
        value["target"]["asset_id"] = str(version.asset_id)
        value["references"][0].update(resource_id=candidate["id"], content_sha256=version.checksum)
        with pytest.raises(DomainError) as exc:
            validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
        assert exc.value.code == "REVIEW_REFERENCE_MISSING"
        value["references"].append(
            {
                "reference_id": str(uuid4()),
                "role": "REFERENCE",
                "resource_id": reference["id"],
                "asset_id": source["id"],
                "asset_type": "CHARACTER",
                "content_sha256": reference["checksum"],
            }
        )
        validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")


def test_existing_script_and_storyboard_content_dependencies_can_be_reviewed_exactly(
    client, review_case
):
    from test_generation_delivery import deliver
    from test_generation_jobs import capture_ok, payload

    from arclight.persistence.models import GenerationAttempt, Storyboard
    from arclight.providers.media_submission import MockMediaSubmissionProvider

    with client.app.state.session_factory() as db:
        board = db.get(Storyboard, UUID(review_case["target"]["storyboard_id"]))
        script, board_version = [
            db.get(AssetVersion, identity) for identity in (board.script_version_id, board.id)
        ]
        script_ref = {
            "reference_id": str(uuid4()),
            "role": "REFERENCE",
            "resource_id": str(script.id),
            "asset_id": str(script.asset_id),
            "asset_type": "SCRIPT",
            "content_sha256": script.checksum,
        }
        input_ids = [str(script.id), str(board_version.id)]
    job = capture_ok(
        client,
        review_case["project_id"],
        payload(
            inputs=[
                {
                    "source_asset_version_id": identity,
                    "dependency_type": "CONTENT_DEPENDENCY",
                }
                for identity in input_ids
            ]
        ),
    )
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 1
    candidate = candidates(client, review_case["project_id"], job)[0]
    value = deepcopy(review_case)
    value["result_id"] = str(uuid4())
    value["target"].update(
        candidate_id=candidate["id"],
        asset_version_id=candidate["asset_version_id"],
        input_manifest_id=candidate["input_manifest_id"],
        media_check_sha256=digest(candidate["media_check"]),
    )
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(candidate["asset_version_id"]))
        value["target"]["asset_id"] = str(version.asset_id)
        value["references"][0].update(resource_id=candidate["id"], content_sha256=version.checksum)
        with pytest.raises(DomainError) as exc:
            validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
        assert exc.value.code == "REVIEW_REFERENCE_MISSING"
        value["references"].append(script_ref)
        validate_review_contract(db, ContinuityReview.model_validate(value), "service-owner")
    assert saved(client, value)[1] is True


def test_invalid_historical_receipt_formats_fail_explicitly_and_never_upgrade(client, review_case):
    from arclight.application.production_versions import bind

    original, _ = saved(client, review_case)
    with client.app.state.session_factory.begin() as db:
        snapshot = deepcopy(
            find_binding(
                db, UUID(review_case["project_id"]), REVIEW_RESOURCE, original.review.result_id
            ).snapshot_json
        )
        for fault in ("identity", "schema", "input_digest", "authority"):
            invalid = deepcopy(snapshot)
            identity = uuid4()
            if fault == "identity":
                invalid["review_contract"]["review"]["project_id"] = str(uuid4())
            elif fault == "schema":
                invalid["review_contract"]["schema_version"] = "continuity-review-record/99"
            elif fault == "input_digest":
                invalid["review_contract"]["input_sha256"] = "0" * 64
            else:
                invalid["review_contract"]["result"]["requires_human_review"] = False
            bind(db, UUID(review_case["project_id"]), REVIEW_RESOURCE, identity, invalid)
            with pytest.raises(DomainError) as exc:
                require_review_record(
                    db, UUID(review_case["project_id"]), identity, "service-owner"
                )
            assert exc.value.code == "REVIEW_RECORD_INVALID"


def test_returned_contract_and_input_mutation_cannot_rewrite_the_saved_artifact(
    client, review_case
):
    record, _ = saved(client, review_case)
    original = record.model_dump(mode="json")
    record.review.references.clear()
    review_case["summary"] = "调用方改写"
    with client.app.state.session_factory() as db:
        restored = require_review_record(
            db,
            UUID(original["review"]["project_id"]),
            UUID(original["review"]["result_id"]),
            "service-owner",
        )
        assert restored.model_dump(mode="json") == original
    with pytest.raises(ValidationError):
        ReviewContractRecord.model_validate(dict(original, sequence=True))
    with pytest.raises(ValidationError):
        ReviewContractRecord.model_validate(dict(original, recorded_at="2026-11-07T00:00:00"))


def test_postgres_competing_reruns_revalidate_the_parent_after_serialization(
    client, review_case, settings
):
    require_postgres(settings)
    original, _ = saved(client, review_case)
    values = []
    for _ in range(2):
        value = deepcopy(review_case)
        value["result_id"] = str(uuid4())
        value["rerun"] = {
            "previous_review_id": review_case["result_id"],
            "previous_sha256": digest(original.model_dump(mode="json")),
            "reason": "并发重查",
        }
        values.append(ContinuityReview.model_validate(value))
    barrier = Barrier(2)

    def execute(review):
        barrier.wait(timeout=15)
        try:
            with client.app.state.session_factory.begin() as db:
                record, created = save_review_contract(db, review, "service-owner")
                assert created and record.sequence == 2
            return "SAVED"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, values))
    assert sorted(results) == ["REVIEW_RERUN_CONFLICT", "SAVED"]
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == REVIEW_RESOURCE)
            )
            == 2
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "continuity_review.contract_saved")
            )
            == 2
        )
