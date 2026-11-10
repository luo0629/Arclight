"""Formal exports use FLOW-03's durable decision and separate resumption journal."""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from render_admission_helpers import (
    assert_blocked,
    ready_manifest,
    render_path,
    request_export,
)
from render_manifest_helpers import render_case, review_case, timeline_case
from sqlalchemy import func, select
from test_human_reviews import decide, decision_payload, resume

from arclight.domain.versions import digest
from arclight.main import create_app
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import AssetVersion, AuditEvent, OutboxEvent
from arclight.persistence.render_manifests import RenderArtifact, RenderReportLink

__all__ = ["render_case", "review_case", "timeline_case"]


def test_exact_formal_review_requires_resume_and_survives_restart(client, settings, render_case):
    value, _ = ready_manifest(client, render_case, profile="FORMAL")
    row = request_export(client, value)
    frozen = row["snapshot"]
    assert frozen["kind"] == "EXPORT" and frozen["purpose"] == "PRODUCTION"
    assert frozen["version_id"] == frozen["object_id"] == value["id"]
    assert frozen["content_sha256"] == value["content_sha256"]
    assert frozen["run_id"] is None and frozen["thread_id"] == f"render:{value['id']}"
    assert set(frozen["source_version_ids"]) == {
        render_case["id"],
        *(item["asset_version_id"] for item in value["snapshot"]["sources"]),
    }
    assert row["request_sha256"] == digest(frozen)
    assert request_export(client, value) == row
    assert_blocked(client.post(render_path(value, "admission")), "RENDER_EXPORT_REVIEW_REQUIRED")
    saved = decide(client, row)
    assert saved.status_code == 200, saved.text
    assert decide(client, row).json() == saved.json()
    assert_blocked(decide(client, row, "REJECT"), "REVIEW_DECISION_CONFLICT")
    assert_blocked(client.post(render_path(value, "admission")), "RENDER_EXPORT_REVIEW_REQUIRED")
    with client.app.state.session_factory() as db:
        before = db.scalar(select(func.count(OutboxEvent.id)))
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        first = resume(restarted, row, saved.json())
        assert first.status_code == 200, first.text
        assert resume(restarted, row, saved.json()).json() == first.json()
        assert first.json()["result_json"]["status"] == "APPROVED"
        admitted = restarted.post(render_path(value, "admission"))
        assert admitted.status_code == 200, admitted.text
        assert admitted.json()["review_request_id"] == row["id"]
        assert admitted.json()["decision_id"] == saved.json()["id"]
        assert admitted.json()["profile"] == "FORMAL"
        assert admitted.json()["is_delivery"] is False
    with client.app.state.session_factory() as db:
        assert db.get(HumanReviewRequest, UUID(row["id"])) is not None
        for model in (HumanReviewDecision, HumanReviewResume):
            assert (
                db.scalar(select(func.count(model.id)).where(model.request_id == UUID(row["id"])))
                == 1
            )
        for model in (RenderArtifact, RenderReportLink):
            assert db.scalar(select(func.count(model.id))) == 0
        assert db.scalar(select(func.count(OutboxEvent.id))) == before
        for action in ("requested", "decided", "resumed"):
            assert (
                db.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.resource_id == UUID(row["id"]),
                        AuditEvent.action == f"human_review.{action}",
                    )
                )
                == 1
            )


def test_rejected_formal_export_never_passes_admission(client, render_case):
    value, _ = ready_manifest(client, render_case, profile="FORMAL")
    row = request_export(client, value)
    saved = decide(client, row, "REJECT")
    assert saved.status_code == 200, saved.text
    receipt = resume(client, row, saved.json())
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["result_json"]["status"] == "REJECTED"
    assert_blocked(client.post(render_path(value, "admission")), "RENDER_EXPORT_NOT_APPROVED")


def test_preview_cannot_mint_formal_approval_and_cannot_approve_other_manifest(client, render_case):
    preview, _ = ready_manifest(client, render_case)
    row = request_export(client, preview)
    assert row["snapshot"]["purpose"] == "PREVIEW"
    assert_blocked(decide(client, row), "PREVIEW_APPROVAL_FORBIDDEN")
    formal, _ = ready_manifest(client, render_case, profile="FORMAL")
    assert_blocked(client.post(render_path(formal, "admission")), "RENDER_EXPORT_REVIEW_REQUIRED")
    rejected = decide(client, row, "REJECT")
    assert rejected.status_code == 200, rejected.text
    assert resume(client, row, rejected.json()).status_code == 200
    assert client.post(render_path(preview, "admission")).status_code == 200


@pytest.mark.parametrize("field", ["version_id", "request_sha256", "current_snapshot"])
def test_client_cannot_change_precise_review_inputs(client, render_case, field):
    value, _ = ready_manifest(client, render_case, profile="FORMAL")
    row = request_export(client, value)
    body = decision_payload(row)
    body[field] = (
        str(uuid4())
        if field == "version_id"
        else "e" * 64
        if field == "request_sha256"
        else row["snapshot"]
    )
    response = client.post(f"/api/v1/human-reviews/{row['id']}/decisions", json=body)
    assert response.status_code == (422 if field == "current_snapshot" else 409), response.text
    assert_blocked(resume(client, row, {"id": str(uuid4())}), "REVIEW_DECISION_REQUIRED")


@pytest.mark.parametrize("phase", ["DECISION", "RESUME", "ADMISSION"])
def test_changed_revision_invalidates_review_even_after_resume(client, render_case, phase):
    value, _ = ready_manifest(client, render_case, profile="FORMAL")
    row = request_export(client, value)
    saved = decide(client, row).json() if phase != "DECISION" else None
    if phase == "ADMISSION":
        assert resume(client, row, saved).status_code == 200
    source = value["snapshot"]["sources"][0]
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(source["asset_version_id"])).revision += 1
    response = (
        decide(client, row)
        if phase == "DECISION"
        else resume(client, row, saved)
        if phase == "RESUME"
        else client.post(render_path(value, "admission"))
    )
    assert_blocked(response, "REVIEW_INPUT_CHANGED")


def test_export_handlers_only_read_database_facts(client, render_case, monkeypatch):
    value, _ = ready_manifest(client, render_case, profile="FORMAL")
    row = request_export(client, value)

    def forbidden(*args, **kwargs):
        raise AssertionError("FLOW-03 handlers cannot perform object or parser I/O")

    monkeypatch.setattr("arclight.storage.objects.ObjectStorage.read", forbidden)
    monkeypatch.setattr("arclight.media.validation.probe", forbidden)
    monkeypatch.setattr("arclight.application.render_admission.probe", forbidden)
    saved = decide(client, row)
    assert saved.status_code == 200, saved.text
    assert resume(client, row, saved.json()).status_code == 200
