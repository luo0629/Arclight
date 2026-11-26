from uuid import UUID, uuid4

from render_admission_helpers import (
    formal_export_command,
    line_decisions,
    start_formal_export,
)
from render_manifest_helpers import render_case, review_case, timeline_case
from sqlalchemy import func, select
from test_concurrency import race, require_postgres

from arclight.application import formal_export
from arclight.core.errors import DomainError
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import AuditEvent
from arclight.persistence.render_jobs import RenderJob

__all__ = ["render_case", "review_case", "timeline_case"]


def test_formal_export_resumes_after_approval_and_object_error(client, render_case, monkeypatch):
    prepared = start_formal_export(client, render_case)
    assert prepared.status_code == 201, prepared.text
    manifest, review = prepared.json()["manifest"], prepared.json()["review"]
    assert review["status"] == "WAITING_REVIEW"
    assert manifest["snapshot"]["timeline_id"] == render_case["id"]
    assert review["snapshot"]["content_sha256"] == manifest["content_sha256"]

    def unavailable(*_args):
        raise DomainError("OBJECT_UNAVAILABLE", "测试素材暂时不可读取", 409)

    with monkeypatch.context() as scope:
        scope.setattr(formal_export, "validate_render_objects", unavailable)
        unavailable_response = formal_export_command(
            client, manifest, "APPROVE", request_sha256=review["request_sha256"]
        )
    assert unavailable_response.status_code == 409, unavailable_response.text
    assert unavailable_response.json()["error"]["code"] == "OBJECT_UNAVAILABLE"
    current = client.get(f"/api/v1/human-reviews/{review['id']}").json()
    assert current["status"] == "RESUMED"
    assert current["resume"]["decision_id"] == current["decision"]["id"]
    jobs_url = f"/api/v1/render-manifests/{manifest['id']}/jobs"
    assert client.get(jobs_url).json() == []

    continued = formal_export_command(
        client, manifest, "CONTINUE", request_sha256=review["request_sha256"]
    )
    assert continued.status_code == 200, continued.text
    job = continued.json()["job"]
    assert job["status"] == "QUEUED"
    assert continued.json()["review"] == current
    assert client.get(jobs_url).json() == [job]
    repeated = formal_export_command(
        client, manifest, "APPROVE", request_sha256=review["request_sha256"]
    )
    assert repeated.json() == continued.json()


def test_postgres_formal_export_same_intent_writes_one_job_and_retry(client, render_case, settings):
    require_postgres(settings)
    creation_path = f"/api/v1/timelines/{render_case['id']}/formal-export"
    body = {
        "creation_key": str(uuid4()),
        "line_audio_decisions": line_decisions(client, render_case),
    }
    headers = dict(client.headers)
    preparations = race(client.app, creation_path, body, headers)
    assert [response.status_code for response in preparations] == [201, 201], [
        response.text for response in preparations
    ]
    assert preparations[0].json() == preparations[1].json()
    manifest, review = preparations[0].json()["manifest"], preparations[0].json()["review"]
    command_path = f"/api/v1/render-manifests/{manifest['id']}/formal-export"
    approvals = race(
        client.app,
        command_path,
        {"action": "APPROVE", "request_sha256": review["request_sha256"]},
        headers,
    )
    assert [response.status_code for response in approvals] == [200, 200], [
        response.text for response in approvals
    ]
    assert approvals[0].json() == approvals[1].json()
    job = approvals[0].json()["job"]
    cancelled = client.post(
        f"/api/v1/render-jobs/{job['id']}/cancel", json={"expected_revision": job["revision"]}
    )
    assert cancelled.status_code == 200, cancelled.text
    retries = race(client.app, command_path, {"action": "RETRY", "retry_of": job["id"]}, headers)
    assert [response.status_code for response in retries] == [200, 200], [
        response.text for response in retries
    ]
    assert retries[0].json() == retries[1].json()
    retry = retries[0].json()["job"]
    assert retry["id"] != job["id"] and retry["retry_of_id"] == job["id"]

    request_id = UUID(review["id"])
    with client.app.state.session_factory() as db:
        assert list(
            db.scalars(
                select(HumanReviewRequest.id).where(
                    HumanReviewRequest.creation_key == f"render:{manifest['id']}"
                )
            )
        ) == [request_id]
        for model in (HumanReviewDecision, HumanReviewResume):
            assert (
                db.scalar(select(func.count(model.id)).where(model.request_id == request_id)) == 1
            )
        for action in ("human_review.requested", "human_review.decided", "human_review.resumed"):
            assert (
                db.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.resource_id == request_id, AuditEvent.action == action
                    )
                )
                == 1
            )
        assert (
            db.scalar(
                select(func.count(RenderJob.id)).where(
                    RenderJob.manifest_id == UUID(manifest["id"])
                )
            )
            == 2
        )
