"""Formal exports use FLOW-03's durable decision and separate resumption journal."""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from render_admission_helpers import (
    assert_blocked,
    formal_export_command,
    ready_manifest,
    record_export_decision,
    record_export_resume,
    request_export,
    start_formal_export,
)
from render_manifest_helpers import render_case, review_case, timeline_case
from sqlalchemy import func, select

from arclight.application.render_admission import admit_render
from arclight.core.errors import DomainError
from arclight.domain.versions import digest
from arclight.main import create_app
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import AssetVersion, AuditEvent, OutboxEvent, RenderJob
from arclight.persistence.render_manifests import RenderArtifact, RenderReportLink

__all__ = ["render_case", "review_case", "timeline_case"]


def test_exact_formal_review_requires_resume_and_survives_restart(client, settings, render_case):
    creation_key = str(uuid4())
    started = start_formal_export(client, render_case, creation_key=creation_key)
    assert started.status_code == 201, started.text
    value, row = started.json()["manifest"], started.json()["review"]
    frozen = row["snapshot"]
    assert row["status"] == "WAITING_REVIEW"
    assert row["decision"] is None and row["resume"] is None
    assert frozen["kind"] == "EXPORT" and frozen["purpose"] == "PRODUCTION"
    assert frozen["version_id"] == frozen["object_id"] == value["id"]
    assert frozen["content_sha256"] == value["content_sha256"]
    assert frozen["run_id"] is None and frozen["thread_id"] == f"render:{value['id']}"
    assert set(frozen["source_version_ids"]) == {
        render_case["id"],
        *(item["asset_version_id"] for item in value["snapshot"]["sources"]),
    }
    sha = row["request_sha256"]
    assert sha == digest(frozen)
    replay = start_formal_export(client, render_case, creation_key=creation_key)
    assert replay.status_code == 201, replay.text
    assert replay.json() == started.json()
    assert_blocked(
        formal_export_command(client, value, "CONTINUE", request_sha256=sha),
        "RENDER_EXPORT_REVIEW_REQUIRED",
    )
    saved = record_export_decision(client, row)
    with client.app.state.session_factory() as db:
        with pytest.raises(DomainError) as pending:
            admit_render(db, settings, UUID(value["id"]), "service-owner")
        assert pending.value.code == "RENDER_EXPORT_REVIEW_REQUIRED"
        before = db.scalar(select(func.count(OutboxEvent.id)))
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        first = formal_export_command(restarted, value, "CONTINUE", request_sha256=sha)
        assert first.status_code == 200, first.text
        response = first.json()
        assert response["review"]["id"] == row["id"]
        assert response["review"]["status"] == "RESUMED"
        assert response["review"]["decision"] == saved
        assert response["review"]["resume"]["decision_id"] == saved["id"]
        assert response["review"]["resume"]["result_json"]["status"] == "APPROVED"
        assert response["job"]["manifest_id"] == value["id"]
        assert response["job"]["manifest_sha256"] == value["content_sha256"]
        assert response["job"]["profile"] == "FORMAL"
        assert response["job"]["is_delivery"] is False
        repeated = formal_export_command(restarted, value, "CONTINUE", request_sha256=sha)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == response
        assert_blocked(
            formal_export_command(restarted, value, "REJECT", request_sha256=sha),
            "REVIEW_DECISION_CONFLICT",
        )
    with client.app.state.session_factory() as db:
        assert db.get(HumanReviewRequest, UUID(row["id"])) is not None
        for model in (HumanReviewDecision, HumanReviewResume):
            assert (
                db.scalar(select(func.count(model.id)).where(model.request_id == UUID(row["id"])))
                == 1
            )
        assert (
            db.scalar(
                select(func.count(RenderJob.id)).where(RenderJob.manifest_id == UUID(value["id"]))
            )
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
    started = start_formal_export(client, render_case)
    assert started.status_code == 201, started.text
    value, row = started.json()["manifest"], started.json()["review"]
    sha = row["request_sha256"]
    rejected = formal_export_command(client, value, "REJECT", request_sha256=sha)
    assert rejected.status_code == 200, rejected.text
    result = rejected.json()
    assert result["job"] is None
    review = result["review"]
    assert review["id"] == row["id"] and review["status"] == "RESUMED"
    assert review["decision"]["decision_json"]["decision"] == "REJECT"
    assert review["resume"]["decision_id"] == review["decision"]["id"]
    assert review["resume"]["result_json"]["status"] == "REJECTED"
    assert formal_export_command(client, value, "REJECT", request_sha256=sha).json() == result
    assert_blocked(
        formal_export_command(client, value, "CONTINUE", request_sha256=sha),
        "RENDER_EXPORT_NOT_APPROVED",
    )
    assert_blocked(
        formal_export_command(client, value, "APPROVE", request_sha256=sha),
        "REVIEW_DECISION_CONFLICT",
    )
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(RenderJob).where(RenderJob.manifest_id == UUID(value["id"]))) is None
        )


def test_preview_cannot_mint_formal_approval_and_cannot_approve_other_manifest(client, render_case):
    preview, _ = ready_manifest(client, render_case)
    row = request_export(client, preview)
    assert row["snapshot"]["purpose"] == "PREVIEW"
    sha = row["request_sha256"]
    assert_blocked(
        formal_export_command(client, preview, "APPROVE", request_sha256=sha),
        "RENDER_FORMAL_EXPORT_REQUIRED",
    )
    with pytest.raises(DomainError) as forbidden:
        record_export_decision(client, row)
    assert forbidden.value.code == "PREVIEW_APPROVAL_FORBIDDEN"
    started = start_formal_export(client, render_case)
    assert started.status_code == 201, started.text
    formal = started.json()["manifest"]
    assert_blocked(
        formal_export_command(client, formal, "APPROVE", request_sha256=sha),
        "REVIEW_INPUT_CHANGED",
    )
    rejected = record_export_decision(client, row, "REJECT")
    receipt = record_export_resume(client, row, rejected)
    assert receipt["result_json"]["status"] == "REJECTED"
    with client.app.state.session_factory() as db:
        admitted = admit_render(db, client.app.state.settings, UUID(preview["id"]), "service-owner")
        assert admitted.profile == "PREVIEW"
        assert admitted.review_request_id is None and admitted.decision_id is None


@pytest.mark.parametrize("field", ["version_id", "request_sha256", "current_snapshot"])
def test_client_cannot_change_precise_review_inputs(client, render_case, field):
    started = start_formal_export(client, render_case)
    assert started.status_code == 201, started.text
    value, row = started.json()["manifest"], started.json()["review"]
    sha = row["request_sha256"]
    overrides = {"request_sha256": sha}
    overrides[field] = (
        str(uuid4())
        if field == "version_id"
        else "e" * 64
        if field == "request_sha256"
        else row["snapshot"]
    )
    response = formal_export_command(client, value, "APPROVE", **overrides)
    if field == "request_sha256":
        assert_blocked(response, "REVIEW_INPUT_CHANGED")
    else:
        assert response.status_code == 422, response.text
    forged = formal_export_command(
        client, value, "CONTINUE", request_sha256=sha, decision_id=str(uuid4())
    )
    assert forged.status_code == 422, forged.text
    assert_blocked(
        formal_export_command(client, value, "CONTINUE", request_sha256=sha),
        "RENDER_EXPORT_REVIEW_REQUIRED",
    )


@pytest.mark.parametrize("phase", ["DECISION", "RESUME", "ADMISSION"])
def test_changed_revision_invalidates_review_even_after_resume(client, render_case, phase):
    started = start_formal_export(client, render_case)
    assert started.status_code == 201, started.text
    value, row = started.json()["manifest"], started.json()["review"]
    sha = row["request_sha256"]
    if phase != "DECISION":
        saved = record_export_decision(client, row)
        if phase == "ADMISSION":
            record_export_resume(client, row, saved)
    source = value["snapshot"]["sources"][0]
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(source["asset_version_id"])).revision += 1
    action = "APPROVE" if phase == "DECISION" else "CONTINUE"
    assert_blocked(
        formal_export_command(client, value, action, request_sha256=sha), "REVIEW_INPUT_CHANGED"
    )
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(RenderJob).where(RenderJob.manifest_id == UUID(value["id"]))) is None
        )


def test_export_handlers_only_read_database_facts(client, render_case, monkeypatch):
    started = start_formal_export(client, render_case)
    assert started.status_code == 201, started.text
    row = started.json()["review"]

    def forbidden(*args, **kwargs):
        raise AssertionError("FLOW-03 handlers cannot perform object or parser I/O")

    monkeypatch.setattr("arclight.storage.objects.ObjectStorage.read", forbidden)
    monkeypatch.setattr("arclight.media.validation.probe", forbidden)
    monkeypatch.setattr("arclight.application.render_admission.probe", forbidden)
    saved = record_export_decision(client, row)
    receipt = record_export_resume(client, row, saved)
    assert receipt["decision_id"] == saved["id"]
    assert receipt["result_json"]["status"] == "APPROVED"
