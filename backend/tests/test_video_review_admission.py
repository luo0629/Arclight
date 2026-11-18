"""VIDEO-01 consumes trusted temporal fixtures through the actual lifecycle API."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_collaboration import register as register_account
from test_generation_video_inputs import video_case as video_case  # noqa: F401
from test_video_candidates import finish_video, imported_rows, video_bytes
from test_video_candidates import formal_video_case as formal_video_case  # noqa: F401

from arclight.application import generation_artifacts
from arclight.application.human_reviews import ReviewHandler, request_review, submit_decision
from arclight.application.production_versions import bind, register
from arclight.application.video_review import read_admission, require_video_review
from arclight.domain.continuity_reviews import review_schema_registration
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.versions import VersionRegistration, digest
from arclight.domain.video_review import (
    VIDEO_ADMISSION_RESOURCE,
    VIDEO_HUMAN_HANDLER,
    VIDEO_REVIEW_RESOURCE,
    VideoReviewRecord,
    VideoReviewReport,
    video_review_schema_registration,
)
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    GenerationCandidate,
    Project,
    ProjectMember,
    VersionBinding,
    VisualAsset,
)


@pytest.fixture
def reviewed_video(client, settings, formal_video_case):
    job, attempt, provider = finish_video(client, settings, formal_video_case, video_bytes())
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 1
    )
    candidate = imported_rows(client)[0]
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, candidate.asset_version_id)
        return {
            **formal_video_case,
            "job": job,
            "candidate_id": candidate.id,
            "version_id": version.id,
            "asset_id": version.asset_id,
            "root": f"/api/v1/projects/{candidate.project_id}/assets/{version.asset_id}",
        }


def report_payload(client, case, *, image_schema=False):
    project_id = UUID(case["project_id"])
    with client.app.state.session_factory.begin() as db:
        candidate = db.get(GenerationCandidate, case["candidate_id"])
        version = db.get(AssetVersion, case["version_id"])
        manifest = db.get(DependencyManifest, candidate.input_manifest_id)
        check = candidate.media_check_json
        parts = []
        for kind in ("AGENT", "PROMPT", "SCHEMA", "MODEL"):
            specification = (
                (
                    review_schema_registration()
                    if image_schema
                    else video_review_schema_registration()
                )
                if kind == "SCHEMA"
                else VersionRegistration(
                    family="REVIEW",
                    kind=kind,
                    name="fixture.video." + kind,
                    version="fixture/1",
                    definition={"origin": "TRUSTED_VIDEO02_FIXTURE", "component": kind},
                )
            )
            row = register(db, project_id, specification)
            parts.append(
                {
                    field: str(getattr(row, field))
                    for field in ("id", "family", "kind", "name", "version", "digest")
                }
            )
        source = version.metadata_json["video_source"]
        return {
            "schema_version": "video-temporal-review/1",
            "report_id": str(uuid4()),
            "project_id": str(project_id),
            "episode_id": case["episode_id"],
            "target": {
                "candidate_id": str(candidate.id),
                "asset_id": str(version.asset_id),
                "asset_version_id": str(version.id),
                "input_manifest_id": str(manifest.id),
                "media_check_sha256": digest(check),
                "checksum": version.checksum,
                "manifest_sha256": digest(manifest.snapshot_json),
                "storyboard_id": source["storyboard_id"],
                "shot_id": source["shot_id"],
            },
            "versions": parts,
            "duration_ms": check["video"]["duration_ms"],
            "sampling_strategy": "BOUNDED_UNIFORM",
            "coverage_start_ms": 0,
            "coverage_end_ms": check["video"]["duration_ms"],
            "samples": [
                {
                    "time_ms": time_ms,
                    "frame_sha256": digest({"fixture_frame": time_ms}),
                    "observation": "合成测试片段中人物与动作连续",
                }
                for time_ms in (0, 3000, 5958)
            ],
            "limitations": ["可信领域路由夹具，仅验证准入消费，不证明真实Agent质量"],
            "conclusion": "PASS",
            "summary": "精确合成视频的时序验收夹具",
            "issues": [],
        }


def write_report(client, value, *, admit=True, admission_changes=None, previous=None):
    """Fixture writes future trusted router output; no public PASS endpoint exists."""
    with client.app.state.session_factory() as db:
        actor = db.get(Project, UUID(value["project_id"])).owner_id
    record = {
        "schema_version": "video-temporal-review-record/1",
        "report": deepcopy(value),
        "input_sha256": digest(
            {field: value[field] for field in ("project_id", "episode_id", "target", "versions")}
        ),
        "report_sha256": digest(value),
        "sequence": previous["sequence"] + 1 if previous else 1,
        "previous_report_id": previous["report"]["report_id"] if previous else None,
        "previous_record_sha256": digest(previous) if previous else None,
        "recorded_by": actor,
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    envelope = {
        "schema_version": 1,
        "compatibility": "EXACT_DEFINITIONS",
        "components": deepcopy(value["versions"]),
        "parameters": {},
        "parameter_sha256": digest({}),
    }
    with client.app.state.session_factory.begin() as db:
        bind(
            db,
            UUID(value["project_id"]),
            VIDEO_REVIEW_RESOURCE,
            UUID(value["report_id"]),
            envelope | {"video_review": record},
        )
        if admit:
            admission = {
                "schema_version": "video-temporal-admission/1",
                "report_id": value["report_id"],
                "record_sha256": digest(record),
                "requires_human_review": False,
                "human_review_request_id": None,
                **(admission_changes or {}),
            }
            bind(
                db,
                UUID(value["project_id"]),
                VIDEO_ADMISSION_RESOURCE,
                UUID(value["report_id"]),
                envelope | {"video_admission": admission},
            )
    return record


def lifecycle(client, case, status):
    root = case["root"] + "/versions/" + str(case["version_id"])
    version = client.get(root).json()
    return client.patch(
        root + "/state",
        json={
            "expected_revision": version["revision"],
            "status": status,
        },
    )


@pytest.mark.parametrize("action", ["READY_FOR_REVIEW", "APPROVED", "select", "lock"])
def test_missing_temporal_report_blocks_every_formal_video_gate(client, reviewed_video, action):
    case = reviewed_video
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, case["version_id"])
        asset = db.get(VisualAsset, case["asset_id"])
        version.status = (
            "READY_FOR_REVIEW"
            if action == "APPROVED"
            else ("APPROVED" if action in {"select", "lock"} else "DRAFT")
        )
        if action == "lock":
            asset.current_version_id = version.id
        before = (asset.current_version_id, asset.revision, asset.lock_version, version.status)
    if action in {"select", "lock"}:
        asset = client.get(case["root"]).json()
        response = client.post(
            case["root"] + f"/versions/{case['version_id']}/{action}",
            json={
                "expected_revision": asset["revision"],
                "expected_lock_version": asset["lock_version"],
            },
        )
    else:
        response = lifecycle(client, case, action)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "VIDEO_REVIEW_REQUIRED"
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, case["version_id"])
        asset = db.get(VisualAsset, case["asset_id"])
        assert (
            asset.current_version_id,
            asset.revision,
            asset.lock_version,
            version.status,
        ) == before
        assert read_admission(db, asset.project_id, version)["status"] == "BLOCKED"


def test_trusted_pass_and_owner_complete_real_ready_approval_selection_lock(client, reviewed_video):
    case = reviewed_video
    value = report_payload(client, case)
    report = write_report(client, value)
    assert VideoReviewRecord.model_validate(report).report == VideoReviewReport.model_validate(
        value
    )
    for status in ("READY_FOR_REVIEW", "APPROVED"):
        response = lifecycle(client, case, status)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == status
    asset = client.get(case["root"]).json()
    selected = client.post(
        case["root"] + f"/versions/{case['version_id']}/select",
        json={
            "expected_revision": asset["revision"],
            "expected_lock_version": asset["lock_version"],
        },
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["current_version_id"] == str(case["version_id"])
    locked = client.post(
        case["root"] + f"/versions/{case['version_id']}/lock",
        json={
            "expected_revision": selected.json()["revision"],
            "expected_lock_version": selected.json()["lock_version"],
        },
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["status"] == "LOCKED"
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, case["version_id"])
        summary = read_admission(db, UUID(case["project_id"]), version)
        assert summary["status"] == "ADMITTED" and summary["sampled_frame_count"] == 3
        assert summary["report_id"] == value["report_id"]
        assert require_video_review(db, version).report.report_id == UUID(value["report_id"])


@pytest.mark.parametrize(
    "problem",
    [
        "image_schema",
        "image_report",
        "first_frame",
        "first_frame_coverage",
        "checksum",
        "version",
        "manifest",
        "check",
        "board",
        "shot",
        "episode",
        "unresolved",
    ],
)
def test_untrusted_stale_or_incomplete_temporal_evidence_cannot_promote_video(
    client,
    reviewed_video,
    problem,
):
    case = reviewed_video
    value = report_payload(client, case, image_schema=problem == "image_schema")
    if problem == "image_report":
        value["schema_version"] = "continuity-review/1"
    elif problem == "first_frame":
        value["samples"] = value["samples"][:1]
    elif problem == "first_frame_coverage":
        value["samples"][-1]["time_ms"] = 3100
    elif problem in {"checksum", "manifest", "check"}:
        value["target"][
            {"manifest": "manifest_sha256", "check": "media_check_sha256"}.get(problem, problem)
        ] = "0" * 64
    elif problem in {"version", "board", "shot"}:
        value["target"][
            {"version": "asset_version_id", "board": "storyboard_id", "shot": "shot_id"}[problem]
        ] = str(uuid4())
    elif problem == "episode":
        value["episode_id"] = str(uuid4())
    elif problem == "unresolved":
        value["conclusion"] = "HUMAN_REVIEW"
        value["issues"] = [
            {
                "issue_id": str(uuid4()),
                "time_ms": 3000,
                "description": "中途身份漂移尚未修复",
                "resolved": False,
            }
        ]
    write_report(client, value)
    response = lifecycle(client, case, "READY_FOR_REVIEW")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] in {
        "VIDEO_REVIEW_INVALID",
        "VIDEO_REVIEW_TARGET_INVALID",
        "VIDEO_REVISION_REQUIRED",
    }
    assert client.get(case["root"] + f"/versions/{case['version_id']}").json()["status"] == "DRAFT"


def owner_decision(client, case, record, *, problem=None):
    registry = {
        VIDEO_HUMAN_HANDLER: ReviewHandler(read=lambda _db, value: value, apply=lambda *_: {})
    }
    with client.app.state.session_factory.begin() as db:
        actor = db.get(Project, UUID(case["project_id"])).owner_id
        version = db.get(AssetVersion, case["version_id"])
        candidate = db.get(GenerationCandidate, case["candidate_id"])
        manifest = db.get(DependencyManifest, candidate.input_manifest_id)
        value = {
            "project_id": UUID(case["project_id"]),
            "episode_id": UUID(case["episode_id"]),
            "run_id": None,
            "thread_id": "fixture.video:" + record["report"]["report_id"],
            "kind": "EXCEPTIONAL_SHOT",
            "object_id": UUID(record["report"]["report_id"]),
            "version_id": version.id,
            "content_sha256": version.checksum,
            "input_sha256": digest(record),
            "source_version_ids": [
                UUID(item["source_asset_version_id"]) for item in manifest.snapshot_json["inputs"]
            ],
        }
        if problem == "report":
            value["object_id"] = uuid4()
        elif problem == "version":
            value["version_id"] = uuid4()
        elif problem == "digest":
            value["input_sha256"] = "0" * 64
        request = request_review(
            db,
            actor,
            str(uuid4()),
            VIDEO_HUMAN_HANDLER,
            ReviewSnapshot.model_validate(value),
            registry=registry,
        )
        if problem != "missing":
            submit_decision(
                db,
                request.id,
                actor,
                HumanDecision(
                    version_id=value["version_id"],
                    request_sha256=request.request_sha256,
                    decision="REJECT" if problem == "reject" else "APPROVE",
                    comment="" if problem == "comment" else "核查精确时序证据并接受本次例外",
                ),
                registry=registry,
            )
        return request.id


@pytest.mark.parametrize(
    "problem", [None, "missing", "reject", "comment", "report", "version", "digest"]
)
def test_video_human_review_requires_current_owner_decision_for_this_exact_report(
    client,
    reviewed_video,
    problem,
):
    case = reviewed_video
    value = report_payload(client, case)
    value["conclusion"] = "HUMAN_REVIEW"
    record = write_report(client, value, admit=False)
    request_id = owner_decision(client, case, record, problem=problem)
    with client.app.state.session_factory.begin() as db:
        bind(
            db,
            UUID(case["project_id"]),
            VIDEO_ADMISSION_RESOURCE,
            UUID(value["report_id"]),
            {
                "schema_version": 1,
                "compatibility": "EXACT_DEFINITIONS",
                "components": value["versions"],
                "parameters": {},
                "parameter_sha256": digest({}),
                "video_admission": {
                    "schema_version": "video-temporal-admission/1",
                    "report_id": value["report_id"],
                    "record_sha256": digest(record),
                    "requires_human_review": True,
                    "human_review_request_id": str(request_id),
                },
            },
        )
    response = lifecycle(client, case, "READY_FOR_REVIEW")
    assert response.status_code == (200 if problem is None else 409), response.text
    if problem is not None:
        assert response.json()["error"]["code"] == "HUMAN_DECISION_REQUIRED"


def test_later_temporal_review_invalidates_previous_pass_without_rewriting_history(
    client, reviewed_video
):
    case = reviewed_video
    first = report_payload(client, case)
    previous = write_report(client, first)
    value = deepcopy(first)
    value.update(report_id=str(uuid4()), conclusion="HUMAN_REVIEW")
    write_report(client, value, admit=False, previous=previous)
    response = lifecycle(client, case, "READY_FOR_REVIEW")
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "VIDEO_REVIEW_REQUIRED"
    )
    with client.app.state.session_factory() as db:
        rows = list(
            db.scalars(
                select(VersionBinding).where(VersionBinding.resource_type == VIDEO_REVIEW_RESOURCE)
            )
        )
        assert len(rows) == 2
        assert any(row.snapshot_json["video_review"] == previous for row in rows)


def test_restore_as_new_gets_own_candidate_check_and_cannot_borrow_old_video_pass(
    client, reviewed_video
):
    case = reviewed_video
    write_report(client, report_payload(client, case))
    asset = client.get(case["root"]).json()
    version_root = case["root"] + f"/versions/{case['version_id']}"
    source = client.get(version_root).json()
    manifest = client.post(
        version_root + "/rebuild-manifest",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": source["revision"],
        },
    )
    assert manifest.status_code == 201, manifest.text
    payload = {
        "creation_key": str(uuid4()),
        "expected_revision": asset["revision"],
        "comment": "按真实原输入另存历史视频",
        "dependency_manifest_id": manifest.json()["id"],
    }
    restored = client.post(version_root + "/restore-as-new", json=payload)
    assert restored.status_code == 201, restored.text
    fresh = restored.json()
    assert fresh["id"] != str(case["version_id"]) and fresh["status"] == "DRAFT"
    assert fresh["restored_from_version_id"] == str(case["version_id"])
    replay = client.post(version_root + "/restore-as-new", json=payload)
    assert replay.status_code == 201 and replay.json()["id"] == fresh["id"]
    with client.app.state.session_factory() as db:
        candidate = db.scalar(
            select(GenerationCandidate).where(
                GenerationCandidate.asset_version_id == UUID(fresh["id"])
            )
        )
        assert candidate.id != case["candidate_id"] and candidate.source == "REPLACEMENT"
        assert candidate.media_check_json["candidate_id"] == str(candidate.id)
        assert candidate.media_check_json["input_manifest_id"] == manifest.json()["id"]
    blocked = lifecycle(client, case | {"version_id": UUID(fresh["id"])}, "READY_FOR_REVIEW")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "VIDEO_REVIEW_REQUIRED"


def test_legacy_video_without_review_metadata_cannot_skip_type_based_gate(client, reviewed_video):
    case = reviewed_video
    with client.app.state.session_factory.begin() as db:
        source = db.get(AssetVersion, case["version_id"])
        version = AssetVersion(
            id=uuid4(),
            asset_id=source.asset_id,
            version_no=source.version_no + 1,
            checksum=source.checksum,
            object_key=source.object_key,
            created_by=source.created_by,
            metadata_json={},
            status="DRAFT",
            stale=False,
        )
        db.add(version)
        db.flush()
        identity = version.id
    response = lifecycle(client, case | {"version_id": identity}, "READY_FOR_REVIEW")
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "VIDEO_REVIEW_REQUIRED"
    )


def test_temporal_pass_does_not_grant_editor_approval_selection_or_lock(client, reviewed_video):
    case = reviewed_video
    write_report(client, report_payload(client, case))
    assert lifecycle(client, case, "READY_FOR_REVIEW").status_code == 200
    owner_header = client.headers["Authorization"]
    editor = register_account(client, "video-editor@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(case["project_id"]),
                user_id=UUID(editor["user"]["id"]),
                role="editor",
            )
        )
    response = lifecycle(client, case, "APPROVED")
    assert response.status_code == 403, response.text
    editor_header = client.headers["Authorization"]
    client.headers["Authorization"] = owner_header
    assert lifecycle(client, case, "APPROVED").status_code == 200
    asset = client.get(case["root"]).json()
    assert asset["current_version_id"] is None
    client.headers["Authorization"] = editor_header
    response = client.post(
        case["root"] + f"/versions/{case['version_id']}/select",
        json={
            "expected_revision": asset["revision"],
            "expected_lock_version": asset["lock_version"],
        },
    )
    assert response.status_code == 403, response.text
    client.headers["Authorization"] = owner_header
    selected = client.post(
        case["root"] + f"/versions/{case['version_id']}/select",
        json={
            "expected_revision": asset["revision"],
            "expected_lock_version": asset["lock_version"],
        },
    ).json()
    client.headers["Authorization"] = editor_header
    response = client.post(
        case["root"] + f"/versions/{case['version_id']}/lock",
        json={
            "expected_revision": selected["revision"],
            "expected_lock_version": selected["lock_version"],
        },
    )
    assert response.status_code == 403, response.text
