"""Real PostgreSQL export-review races use the existing project transaction gate."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from render_admission_helpers import ready_manifest, render_path, request_export
from render_manifest_helpers import render_case, review_case, timeline_case
from sqlalchemy import func, select
from test_concurrency import race, require_postgres
from test_human_reviews import decide, decision_payload

from arclight.application.human_reviews import submit_decision
from arclight.core.errors import DomainError
from arclight.domain.human_reviews import HumanDecision
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import AuditEvent

__all__ = ["render_case", "review_case", "timeline_case"]


@pytest.fixture
def postgres_export(settings, client, request):
    require_postgres(settings)
    timeline = request.getfixturevalue("render_case")
    return ready_manifest(client, timeline, profile="FORMAL")[0]


@pytest.mark.parametrize("operation", ["REQUEST", "DECISION", "RESUME"])
def test_postgres_export_duplicates_keep_one_record_and_audit(client, postgres_export, operation):
    if operation == "REQUEST":
        path, body = render_path(postgres_export, "export-review"), None
        model, action, status = HumanReviewRequest, "requested", 201
    else:
        row = request_export(client, postgres_export)
        if operation == "DECISION":
            path = f"/api/v1/human-reviews/{row['id']}/decisions"
            body = decision_payload(row)
            model, action, status = HumanReviewDecision, "decided", 200
        else:
            decision = decide(client, row)
            assert decision.status_code == 200, decision.text
            path = f"/api/v1/human-reviews/{row['id']}/resume"
            body = {"decision_id": decision.json()["id"], "request_sha256": row["request_sha256"]}
            model, action, status = HumanReviewResume, "resumed", 200
    responses = race(client.app, path, body, dict(client.headers))
    assert [response.status_code for response in responses] == [status, status], [
        response.text for response in responses
    ]
    assert responses[0].json() == responses[1].json()
    request_id = UUID(responses[0].json()["id"] if operation == "REQUEST" else row["id"])
    with client.app.state.session_factory() as db:
        predicate = (
            model.id == request_id if operation == "REQUEST" else model.request_id == request_id
        )
        assert db.scalar(select(func.count(model.id)).where(predicate)) == 1
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_id == request_id,
                    AuditEvent.action == f"human_review.{action}",
                )
            )
            == 1
        )


def test_postgres_conflicting_export_decisions_keep_one_winner(client, postgres_export):
    row = request_export(client, postgres_export)
    barrier = Barrier(2)

    def invoke(choice):
        barrier.wait(timeout=20)
        try:
            with client.app.state.session_factory.begin() as db:
                result = submit_decision(
                    db,
                    UUID(row["id"]),
                    "service-owner",
                    HumanDecision.model_validate(decision_payload(row, choice)),
                )
                return "SAVED", result.decision_json["decision"]
        except DomainError as error:
            return error.code, choice

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ("APPROVE", "REJECT")))
    assert sorted(result[0] for result in results) == ["REVIEW_DECISION_CONFLICT", "SAVED"]
    current = client.get(f"/api/v1/human-reviews/{row['id']}").json()
    assert current["decision"]["decision_json"]["decision"] == next(
        choice for status, choice in results if status == "SAVED"
    )
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count(HumanReviewDecision.id)).where(
                    HumanReviewDecision.request_id == UUID(row["id"])
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_id == UUID(row["id"]),
                    AuditEvent.action == "human_review.decided",
                )
            )
            == 1
        )
