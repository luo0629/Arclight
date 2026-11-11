"""Machine reports bind real Worker output and preserve independent review history."""

import hashlib
import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from render_admission_helpers import ready_manifest
from render_job_helpers import expire, read, run, submit
from render_report_helpers import (
    accept_warnings,
    completed_report,
    delivery_url,
    formal_job,
    formal_report_case,
    no_dialogue_render_case,
    production_state,
    render_case,
    report_bytes,
    report_read,
    report_url,
    request_warnings,
    review_case,
    timeline_case,
)
from sqlalchemy import func, select
from test_human_reviews import decide, resume

from arclight.application import render_execution
from arclight.domain.render_inspections import RenderInspectionResult
from arclight.main import create_app
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import AssetVersion, AuditEvent, StoredFile
from arclight.persistence.render_jobs import RenderAttempt, RenderInspection
from arclight.persistence.render_manifests import RenderArtifact, RenderReportLink
from arclight.storage.objects import ObjectStorage

__all__ = [
    "formal_report_case",
    "no_dialogue_render_case",
    "render_case",
    "review_case",
    "timeline_case",
]


def test_formal_worker_writes_actual_report_bound_to_every_exact_receipt(
    client, episode, formal_report_case
):
    manifest, final, response = formal_report_case
    envelope = response["report"]
    details = envelope["details"]
    data = report_bytes(client, response)
    assert json.loads(data) == envelope
    assert hashlib.sha256(data).hexdigest() == response["report_sha256"]
    assert details["schema_version"] == "render-report/1"
    assert details["checker"] == "arclight-delivery-v1"
    assert details["profile"] == "FORMAL"
    assert details["render_status"] == "passed"
    assert details["job_id"] == final["id"]
    assert final["report_id"] == response["report_id"]
    assert final["delivery_phase"] == "DELIVERY_QA" and final["is_delivery"] is False
    assert details["artifact_id"] == final["artifact_id"]
    assert details["asset_version_id"] == final["output_asset_version_id"]
    assert details["manifest_id"] == envelope["manifest_id"] == manifest["id"]
    assert details["manifest_sha256"] == envelope["manifest_sha256"] == manifest["content_sha256"]
    assert details["timeline_id"] == envelope["timeline_id"] == manifest["snapshot"]["timeline_id"]
    assert details["timeline_sha256"] == manifest["snapshot"]["timeline_sha256"]
    assert details["output_file_id"] == envelope["output_file_id"] == final["output_file_id"]
    for field in ("sources", "font", "software", "output", "line_audio_decisions"):
        assert details[field] == manifest["snapshot"][field]
    checks = details["checks"]
    for field in ("video_decodable", "audio_stream_present", "subtitle_range_valid"):
        assert checks[field] is True
    for field in ("missing_required_shots", "missing_audio_lines", "stale_dependencies"):
        assert type(checks[field]) is int and checks[field] == 0
    technical = checks["technical"]
    inspection_result = RenderInspectionResult.model_validate(technical)
    assert inspection_result.passed is True and inspection_result.error_code is None
    assert all(check.passed for check in inspection_result.checks)
    assert technical["measurement"]["video"]["frames"] == 60
    with client.app.state.session_factory() as db:
        artifact = db.get(RenderArtifact, UUID(final["artifact_id"]))
        version = db.get(AssetVersion, artifact.asset_version_id)
        inspection = db.get(RenderInspection, UUID(details["inspection_id"]))
        attempt = db.get(RenderAttempt, inspection.attempt_id)
        link = db.get(RenderReportLink, UUID(response["report_id"]))
        file = db.get(StoredFile, link.file_id)
        assert str(attempt.id) == details["attempt_id"]
        assert inspection.result_json == technical
        assert details["input_sha256"] == inspection.input_sha256 == attempt.input_sha256
        assert details["output_sha256"] == envelope["output_sha256"] == inspection.output_sha256
        assert artifact.checksum == version.checksum == envelope["output_sha256"]
        assert artifact.byte_size == details["byte_size"] == inspection.byte_size
        assert artifact.duration_ms == details["duration_ms"] == 2500
        assert link.artifact_id == artifact.id
        assert str(file.id) == response["report_file_id"]
        assert file.media_type == "application/json"
        assert file.checksum == link.checksum == response["report_sha256"]
        assert file.byte_size == link.byte_size == len(data)
    assert client.get(delivery_url(episode, final["artifact_id"])).status_code == 200


def test_no_dialogue_formal_render_completes_without_inventing_warning_review(
    client, episode, no_dialogue_render_case
):
    _, final, report = completed_report(client, episode, no_dialogue_render_case)
    assert report["report"]["details"]["line_audio_decisions"] == []
    assert report["report"]["details"]["warnings"] == []
    assert report["acceptance"]["phase"] == "COMPLETED"
    assert final["delivery_phase"] == "COMPLETED" and final["is_delivery"] is True
    assert report["acceptance"]["is_delivery"] is True
    assert report["acceptance"]["warnings_confirmed"] is True
    assert report["acceptance"]["review_id"] is None
    state = production_state(client, episode)
    assert state["phase"] == "COMPLETED", json.dumps(state, ensure_ascii=False)
    detail = client.get(delivery_url(episode, final["artifact_id"])).json()
    assert detail["is_delivery"] is True
    assert detail["acceptance"]["phase"] == "COMPLETED"
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count(HumanReviewRequest.id)).where(
                    HumanReviewRequest.handler == "RENDER_DELIVERY"
                )
            )
            == 0
        )


def test_export_approval_does_not_accept_explicit_silence_and_decision_requires_resume(
    client, episode, formal_report_case
):
    manifest, final, report = formal_report_case
    warnings = report["report"]["details"]["warnings"]
    decisions = manifest["snapshot"]["line_audio_decisions"]
    assert len(warnings) == len(decisions) > 0
    assert len({item["id"] for item in warnings}) == len(warnings)
    assert all(item["code"] == "EXPLICIT_SILENCE" for item in warnings)
    assert {(item["source_version_id"], item["line_id"], item["reason"]) for item in warnings} == {
        (item["script_version_id"], item["line_id"], item["reason"]) for item in decisions
    }
    assert report["acceptance"]["phase"] == "DELIVERY_QA"
    assert not report["acceptance"]["is_delivery"]
    assert not report["acceptance"]["warnings_confirmed"]
    state = production_state(client, episode)
    assert state["phase"] == "DELIVERY_QA", json.dumps(state, ensure_ascii=False)
    original_bytes = report_bytes(client, report)
    requested = request_warnings(client, episode, final["artifact_id"])
    assert request_warnings(client, episode, final["artifact_id"]) == requested
    snapshot = requested["snapshot"]
    assert snapshot["kind"] == "DELIVERY"
    with client.app.state.session_factory() as db:
        request = db.get(HumanReviewRequest, UUID(requested["id"]))
        assert request.handler == "RENDER_DELIVERY"
    assert snapshot["object_id"] == final["artifact_id"]
    assert snapshot["version_id"] == final["output_asset_version_id"]
    assert snapshot["content_sha256"] == report["report_sha256"]
    saved = decide(client, requested)
    assert saved.status_code == 200, saved.text
    assert (
        report_read(client, episode, final["artifact_id"])["acceptance"]["phase"] == "DELIVERY_QA"
    )
    handoff = resume(client, requested, saved.json())
    assert handoff.status_code == 200, handoff.text
    accepted = report_read(client, episode, final["artifact_id"])
    assert accepted["acceptance"]["phase"] == "COMPLETED_WITH_WARNINGS"
    assert accepted["acceptance"]["warnings_confirmed"] and accepted["acceptance"]["is_delivery"]
    assert accepted["acceptance"]["review_id"] == requested["id"]
    current_job = read(client, final)
    assert current_job["delivery_phase"] == "COMPLETED_WITH_WARNINGS"
    assert current_job["is_delivery"] is True
    assert current_job["report_id"] == report["report_id"]
    assert production_state(client, episode)["phase"] == "COMPLETED_WITH_WARNINGS"
    assert accepted["report"] == report["report"]
    assert report_bytes(client, accepted) == original_bytes


def test_rejected_warning_review_never_claims_delivery(client, episode, formal_report_case):
    _, final, report = formal_report_case
    requested = request_warnings(client, episode, final["artifact_id"])
    saved = decide(client, requested, "REJECT")
    assert saved.status_code == 200, saved.text
    handoff = resume(client, requested, saved.json())
    assert handoff.status_code == 200, handoff.text
    assert handoff.json()["result_json"]["status"] == "REJECTED"
    current = report_read(client, episode, final["artifact_id"])
    assert current["acceptance"]["phase"] == "DELIVERY_QA"
    assert not current["acceptance"]["is_delivery"]
    assert current["report"] == report["report"]
    assert decide(client, requested).status_code == 409


def test_warning_decision_and_resume_survive_restart_once_only(
    client, settings, episode, formal_report_case
):
    _, final, _ = formal_report_case
    requested = request_warnings(client, episode, final["artifact_id"])
    saved = decide(client, requested)
    assert saved.status_code == 200, saved.text
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        first = resume(restarted, requested, saved.json())
        assert first.status_code == 200, first.text
        assert resume(restarted, requested, saved.json()).json() == first.json()
        assert decide(restarted, requested).json() == saved.json()
        assert request_warnings(restarted, episode, final["artifact_id"])["id"] == requested["id"]
        assert report_read(restarted, episode, final["artifact_id"])["acceptance"]["is_delivery"]
    with client.app.state.session_factory() as db:
        for model in (HumanReviewDecision, HumanReviewResume):
            assert (
                db.scalar(
                    select(func.count(model.id)).where(model.request_id == UUID(requested["id"]))
                )
                == 1
            )
        for action in ("requested", "decided", "resumed"):
            assert (
                db.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.resource_id == UUID(requested["id"]),
                        AuditEvent.action == f"human_review.{action}",
                    )
                )
                == 1
            )


def test_replay_and_rerender_keep_report_bytes_and_independent_warning_acceptance(
    client, episode, formal_report_case
):
    manifest, first, original = formal_report_case
    original_bytes = report_bytes(client, original)
    accept_warnings(client, episode, first["artifact_id"])
    assert run(client, first) == "SUCCEEDED"
    assert (
        report_bytes(client, report_read(client, episode, first["artifact_id"])) == original_bytes
    )
    next_job, _ = submit(client, manifest)
    assert run(client, next_job) == "SUCCEEDED"
    second = read(client, next_job)
    report = report_read(client, episode, second["artifact_id"])
    assert second["artifact_id"] != first["artifact_id"]
    assert second["output_asset_version_id"] != first["output_asset_version_id"]
    assert report["report_id"] != original["report_id"]
    assert report["report_file_id"] != original["report_file_id"]
    assert report["report_sha256"] != original["report_sha256"]
    assert report["acceptance"]["phase"] == "DELIVERY_QA"
    assert not report["acceptance"]["is_delivery"]
    assert production_state(client, episode)["phase"] == "DELIVERY_QA"
    assert report_read(client, episode, first["artifact_id"])["acceptance"]["is_delivery"]
    assert report_bytes(client, original) == original_bytes
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderReportLink.id))) == 2
        assert db.scalar(select(func.count(RenderArtifact.id))) == 2


def test_preview_keeps_real_inspection_without_machine_delivery_report(
    client, episode, render_case
):
    manifest, _ = ready_manifest(client, render_case, profile="PREVIEW")
    value, _ = submit(client, manifest)
    assert run(client, value) == "SUCCEEDED"
    final = read(client, value)
    detail = client.get(delivery_url(episode, final["artifact_id"])).json()
    assert detail["profile"] == "PREVIEW" and detail["is_delivery"] is False
    assert client.get(report_url(episode, final["artifact_id"])).status_code == 404
    refused = client.post(delivery_url(episode, final["artifact_id"]) + "/warning-review")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "RENDER_WARNINGS_NOT_PENDING"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderReportLink.id))) == 0
        assert db.scalar(select(RenderInspection.passed)) is True


@pytest.mark.parametrize("loss", ["failure", "worker_loss"])
def test_report_publication_rolls_back_registry_and_preserves_inspection_for_recovery(
    client, episode, render_case, monkeypatch, loss
):
    _, value = formal_job(client, render_case)
    register = render_execution.register_machine_report
    reached = []

    def interrupted(*args, **kwargs):
        register(*args, **kwargs)
        reached.append(True)
        if loss == "worker_loss":
            raise SystemExit("worker lost after machine report write")
        raise RuntimeError("machine report transaction rejected")

    with monkeypatch.context() as patch:
        patch.setattr(render_execution, "register_machine_report", interrupted)
        if loss == "worker_loss":
            with pytest.raises(SystemExit, match="worker lost"):
                run(client, value)
        else:
            assert run(client, value) == "FAILED"
    assert reached == [True]
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderArtifact.id))) == 0
        assert db.scalar(select(func.count(RenderReportLink.id))) == 0
        old_inspection = db.scalar(select(RenderInspection))
        assert old_inspection.passed
        old_result = old_inspection.result_json
        old_id = old_inspection.id
    assert client.get(f"/api/v1/episodes/{episode['id']}/deliveries").json() == []
    if loss == "worker_loss":
        expire(client, value)
        assert run(client, value) == "SUCCEEDED"
        final = read(client, value)
        report = report_read(client, episode, final["artifact_id"])
        assert report["report"]["details"]["inspection_id"] != str(old_id)
        with client.app.state.session_factory() as db:
            assert db.get(RenderInspection, old_id).result_json == old_result
            attempts = list(db.scalars(select(RenderAttempt).order_by(RenderAttempt.attempt_no)))
            assert [item.status for item in attempts] == ["INTERRUPTED", "SUCCEEDED"]


def test_machine_report_object_failure_never_publishes_a_successful_film(
    client, episode, render_case, monkeypatch
):
    _, value = formal_job(client, render_case)
    original = ObjectStorage.put
    attempted = []

    def failed_report(store, key, data, media_type):
        if media_type == "application/json" and b"render-report/1" in data:
            attempted.append(key)
            raise RuntimeError("report object storage unavailable")
        return original(store, key, data, media_type)

    monkeypatch.setattr(ObjectStorage, "put", failed_report)
    assert run(client, value) == "FAILED"
    assert len(attempted) == 1
    assert read(client, value)["output_file_id"] is None
    assert client.get(f"/api/v1/episodes/{episode['id']}/deliveries").json() == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderArtifact.id))) == 0
        assert db.scalar(select(func.count(RenderReportLink.id))) == 0
        assert db.scalar(select(RenderInspection.passed)) is True
