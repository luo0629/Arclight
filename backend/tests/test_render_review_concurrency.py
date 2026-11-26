"""Real PostgreSQL export-review races use the existing project transaction gate."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from render_admission_helpers import (
    record_export_decision,
    render_path,
    start_formal_export,
)
from render_manifest_helpers import render_case, review_case, timeline_case
from sqlalchemy import func, select
from test_concurrency import race, require_postgres

from arclight.main import create_app
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewResume,
)
from arclight.persistence.models import AuditEvent

__all__ = ["render_case", "review_case", "timeline_case"]


@pytest.fixture
def postgres_export(settings, client, request):
    require_postgres(settings)
    timeline = request.getfixturevalue("render_case")
    response = start_formal_export(client, timeline)
    assert response.status_code == 201, response.text
    return response.json()["manifest"], response.json()["review"]


def test_postgres_export_duplicate_continue_keeps_one_resume_and_audit(client, postgres_export):
    manifest, review = postgres_export
    decision = record_export_decision(client, review)
    path = render_path(manifest, "formal-export")
    body = {"action": "CONTINUE", "request_sha256": review["request_sha256"]}
    responses = race(client.app, path, body, dict(client.headers))
    assert [response.status_code for response in responses] == [200, 200], [
        response.text for response in responses
    ]
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["review"]["resume"]["decision_id"] == decision["id"]
    request_id = UUID(review["id"])
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count(HumanReviewResume.id)).where(
                    HumanReviewResume.request_id == request_id
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_id == request_id,
                    AuditEvent.action == "human_review.resumed",
                )
            )
            == 1
        )


def test_postgres_conflicting_export_decisions_keep_one_winner(client, postgres_export):
    manifest, review = postgres_export
    path = render_path(manifest, "formal-export")
    headers = dict(client.headers)
    barrier = Barrier(2)

    def invoke(choice):
        with TestClient(create_app(client.app.state.settings)) as connection:
            barrier.wait(timeout=15)
            return connection.post(
                path,
                json={"action": choice, "request_sha256": review["request_sha256"]},
                headers=headers,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        approval, rejection = pool.map(invoke, ("APPROVE", "REJECT"))
    winner, loser = sorted((approval, rejection), key=lambda response: response.status_code)
    assert [winner.status_code, loser.status_code] == [200, 409], [winner.text, loser.text]
    assert loser.json()["error"]["code"] == "REVIEW_DECISION_CONFLICT"
    current = client.get(f"/api/v1/human-reviews/{review['id']}").json()
    assert current == winner.json()["review"]
    assert current["decision"]["decision_json"]["decision"] == (
        "APPROVE" if winner is approval else "REJECT"
    )
    request_id = UUID(review["id"])
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count(HumanReviewDecision.id)).where(
                    HumanReviewDecision.request_id == request_id
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_id == request_id,
                    AuditEvent.action == "human_review.decided",
                )
            )
            == 1
        )
