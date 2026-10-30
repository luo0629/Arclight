"""GEN-04: ambiguous execution never authorizes an unproven resubmission."""

from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_collaboration import create_project, register
from test_generation_attempts import start
from test_generation_jobs import base, capture_ok

from arclight.application.generation_attempts import start_attempt
from arclight.application.generation_reconciliation import mark_result_unknown, reconcile
from arclight.application.generation_state import request_cancel
from arclight.core.errors import DomainError
from arclight.domain.generation_requests import GenerationResult
from arclight.persistence.generation import (
    GenerationAttempt,
    GenerationJob,
    GenerationReconciliation,
)
from arclight.persistence.models import ProjectMember
from arclight.providers.media_query import (
    ProviderQueryError,
    QueryReceipt,
    get_media_query_provider,
)


class QueryFake:
    def __init__(self, receipt=None, action=None):
        self.receipt = receipt or QueryReceipt(status="UNKNOWN")
        self.action = action
        self.calls = []

    def query(self, lookup):
        self.calls.append(lookup)
        if self.action:
            self.action()
        return self.receipt


def unknown(client, project_id, *, request_id="provider-17"):
    job = capture_ok(client, project_id)
    attempt = start(client, job)
    with client.app.state.session_factory.begin() as db:
        row = mark_result_unknown(db, UUID(job["id"]), attempt.id, request_id=request_id)
        job["revision"] = row.revision
    return job, attempt


def query(client, job, provider):
    client.app.dependency_overrides[get_media_query_provider] = lambda: provider
    return client.post(
        base(job["project_id"]) + f"/{job['id']}/reconcile",
        json={"expected_revision": job["revision"]},
    )


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("RUNNING", "RUNNING"),
        ("SUCCEEDED", "SUCCEEDED"),
        ("FAILED", "FAILED"),
        ("CANCELLED", "CANCELLED"),
        ("NOT_ACCEPTED", "RETRY_WAIT"),
        ("UNKNOWN", "MANUAL_REVIEW"),
        ("NOT_FOUND", "MANUAL_REVIEW"),
        ("UNSUPPORTED", "MANUAL_REVIEW"),
    ],
)
def test_query_confirms_only_authoritative_outcomes(client, episode, outcome, expected):
    job, attempt = unknown(client, episode["project_id"])
    receipt = QueryReceipt(
        status=outcome,
        result=GenerationResult(output_handles=["image-17"]) if outcome == "SUCCEEDED" else None,
    )
    provider = QueryFake(receipt)
    response = query(client, job, provider)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected
    assert provider.calls[0].request_id == "provider-17"
    history = client.get(base(job["project_id"]) + f"/{job['id']}/reconciliations").json()
    assert len(history) == 1 and history[0]["receipt_json"]["status"] == outcome
    assert history[0]["disposition"] == "APPLIED" and history[0]["finished_at"].endswith("Z")
    with client.app.state.session_factory.begin() as db:
        row = db.get(GenerationAttempt, attempt.id)
        assert row.attempt_no == 1 and row.cost is None
        assert (row.finished_at is not None) == (
            outcome in {"SUCCEEDED", "FAILED", "CANCELLED", "NOT_ACCEPTED"}
        )
        if expected == "RETRY_WAIT":
            new = start_attempt(db, UUID(job["id"]), response.json()["revision"])
            assert new.attempt_no == 2
        else:
            with pytest.raises(DomainError):
                start_attempt(db, UUID(job["id"]), response.json()["revision"])


@pytest.mark.parametrize("error", [TimeoutError, ProviderQueryError, ConnectionError])
def test_query_error_is_redacted_and_never_means_not_accepted(client, episode, error):
    job, _ = unknown(client, episode["project_id"])

    def fail():
        raise error("Authorization: Bearer private-secret https://private-user-media")

    response = query(client, job, QueryFake(action=fail))
    assert response.status_code == 200 and response.json()["status"] == "MANUAL_REVIEW"
    history = client.get(base(job["project_id"]) + f"/{job['id']}/reconciliations")
    assert "QUERY_FAILED" in history.text and "private-secret" not in history.text


def test_missing_request_identity_does_not_invent_a_provider_lookup(client, episode):
    job, _ = unknown(client, episode["project_id"], request_id=None)
    provider = QueryFake(QueryReceipt(status="NOT_ACCEPTED"))
    response = query(client, job, provider)
    assert response.status_code == 200 and response.json()["status"] == "MANUAL_REVIEW"
    assert provider.calls == []


def test_query_releases_locks_and_cancel_race_preserves_uncertainty(client, episode):
    job, attempt = unknown(client, episode["project_id"])
    factory = client.app.state.session_factory

    def cancel():
        # A separate real transaction must commit while the provider query is active.
        with factory.begin() as db:
            row = db.get(GenerationJob, UUID(job["id"]))
            request_cancel(db, row.id, row.revision, "service-owner")

    provider = QueryFake(
        QueryReceipt(status="SUCCEEDED", result=GenerationResult(output_handles=["late-image"])),
        cancel,
    )
    response = query(client, job, provider)
    assert response.status_code == 200, response.text
    assert response.json()["cancel_requested_at"] is not None
    assert response.json()["status"] == "RECONCILING"
    with factory() as db:
        fact = db.scalar(select(GenerationReconciliation))
        assert fact.disposition == "SUPERSEDED" and fact.receipt_json["status"] == "SUCCEEDED"
        assert db.get(GenerationAttempt, attempt.id).finished_at is None
    job["revision"] = response.json()["revision"]
    confirmed = query(
        client,
        job,
        QueryFake(
            QueryReceipt(status="SUCCEEDED", result=GenerationResult(output_handles=["late-image"]))
        ),
    )
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "SUCCEEDED"


def test_newer_query_wins_and_old_receipt_remains_as_history(client, episode):
    job, _ = unknown(client, episode["project_id"])
    factory = client.app.state.session_factory

    def newer():
        with factory() as db:
            revision = db.get(GenerationJob, UUID(job["id"])).revision
        reconcile(
            factory,
            UUID(job["project_id"]),
            UUID(job["id"]),
            revision,
            "service-owner",
            QueryFake(
                QueryReceipt(
                    status="SUCCEEDED", result=GenerationResult(output_handles=["new-result"])
                )
            ),
        )

    response = query(client, job, QueryFake(QueryReceipt(status="NOT_ACCEPTED"), newer))
    assert response.status_code == 200 and response.json()["status"] == "SUCCEEDED"
    with factory() as db:
        facts = list(
            db.scalars(
                select(GenerationReconciliation).order_by(GenerationReconciliation.claimed_revision)
            )
        )
        assert [fact.disposition for fact in facts] == ["SUPERSEDED", "APPLIED"]
        assert db.scalar(select(GenerationAttempt)).result_json["output_handles"] == ["new-result"]


def test_unexpected_adapter_crash_is_recoverable_without_submitting(client, episode):
    job, _ = unknown(client, episode["project_id"])
    factory = client.app.state.session_factory

    def crash():
        raise RuntimeError("adapter bug")

    with pytest.raises(RuntimeError, match="adapter bug"):
        reconcile(
            factory,
            UUID(job["project_id"]),
            UUID(job["id"]),
            job["revision"],
            "service-owner",
            QueryFake(action=crash),
        )
    with factory() as db:
        assert db.scalar(select(GenerationReconciliation)).disposition == "PENDING"
        job["revision"] = db.get(GenerationJob, UUID(job["id"])).revision
    response = query(client, job, QueryFake(QueryReceipt(status="RUNNING")))
    assert response.status_code == 200 and response.json()["status"] == "RUNNING"
    with factory() as db:
        assert db.get(GenerationJob, UUID(job["id"])).attempt_count == 1


def test_manual_resolution_requires_owner_explicit_confirmation_and_evidence(client, episode):
    job, _ = unknown(client, episode["project_id"])
    url = base(job["project_id"]) + f"/{job['id']}/resolve"
    body = {
        "expected_revision": job["revision"],
        "confirmed": False,
        "evidence_reference": "support-ticket:19",
        "receipt": {"status": "NOT_ACCEPTED"},
    }
    assert client.post(url, json=body).status_code == 422
    assert (
        client.post(
            url, json=body | {"confirmed": True, "receipt": {"status": "NOT_FOUND"}}
        ).status_code
        == 422
    )
    response = client.post(url, json=body | {"confirmed": True})
    assert response.status_code == 200 and response.json()["status"] == "RETRY_WAIT"
    facts = client.get(url.replace("/resolve", "/reconciliations")).json()
    assert facts[0]["source"] == "MANUAL" and facts[0]["evidence_reference"] == "support-ticket:19"


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_query_and_manual_permissions_and_project_scope(client, role):
    register(client)
    project = create_project(client)
    job, _ = unknown(client, project["id"])
    member = register(client, f"query-{role}@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    provider = QueryFake()
    response = query(client, job, provider)
    assert response.status_code == {"editor": 200, "viewer": 403, "outsider": 404}[role]
    assert len(provider.calls) == (1 if role == "editor" else 0)
    url = base(project["id"]) + f"/{job['id']}"
    assert client.get(url + "/reconciliations").status_code == (404 if role == "outsider" else 200)
    assert client.post(
        url + "/resolve",
        json={
            "expected_revision": 0,
            "confirmed": True,
            "evidence_reference": "ticket:1",
            "receipt": {"status": "NOT_ACCEPTED"},
        },
    ).status_code == (404 if role == "outsider" else 403)


def test_receipt_history_cannot_be_overwritten_deleted_or_replaced(client, episode):
    job, _ = unknown(client, episode["project_id"])
    assert query(client, job, QueryFake()).status_code == 200
    factory = client.app.state.session_factory
    with factory() as db:
        fact = db.scalar(select(GenerationReconciliation))
        identity = fact.id
    for change in (
        "UPDATE generation_reconciliations SET actor = 'other'",
        "UPDATE generation_reconciliations SET disposition = 'SUPERSEDED'",
        "DELETE FROM generation_reconciliations",
    ):
        with pytest.raises(DBAPIError), factory.begin() as db:
            db.execute(text(change))
    with factory() as db:
        dialect = db.get_bind().dialect.name
    sql = (
        "INSERT OR REPLACE INTO generation_reconciliations SELECT * FROM generation_reconciliations"
        if dialect == "sqlite"
        else "TRUNCATE generation_reconciliations"
    )
    with pytest.raises(DBAPIError), factory.begin() as db:
        db.execute(text(sql))
    with factory() as db:
        assert db.get(GenerationReconciliation, identity).disposition == "APPLIED"


def test_query_receipt_rejects_unsafe_or_inconsistent_results():
    for body in [
        {"status": "SUCCEEDED"},
        {"status": "UNKNOWN", "result": {"output_handles": ["image"]}},
        {"status": "SUCCEEDED", "result": {"output_handles": ["https://private/token"]}},
        {"status": "NOT_FOUND", "cost": "0"},
        {"status": "FAILED", "cost": Decimal("NaN")},
    ]:
        with pytest.raises(ValidationError):
            QueryReceipt.model_validate(body)


def test_definite_usage_stale_revision_and_unknown_retry_gate(client, episode):
    job, attempt = unknown(client, episode["project_id"])
    provider = QueryFake(QueryReceipt(status="FAILED", cost=Decimal("0.123456"), latency_ms=52))
    stale = dict(job, revision=0)
    assert query(client, stale, provider).status_code == 409
    assert provider.calls == []
    url = base(job["project_id"]) + f"/{job['id']}"
    assert (
        client.post(url + "/retry", json={"expected_revision": job["revision"]}).status_code == 409
    )
    response = query(client, job, provider)
    assert response.status_code == 200 and response.json()["status"] == "FAILED"
    with client.app.state.session_factory() as db:
        row = db.get(GenerationAttempt, attempt.id)
        assert row.cost == Decimal("0.123456") and row.latency_ms == 52


def test_ordinary_not_found_keeps_cancel_intent_and_never_retries(client, episode):
    job, _ = unknown(client, episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        row = request_cancel(db, UUID(job["id"]), job["revision"], "service-owner")
        job["revision"] = row.revision
    response = query(client, job, QueryFake(QueryReceipt(status="NOT_FOUND")))
    assert response.status_code == 200 and response.json()["status"] == "MANUAL_REVIEW"
    assert response.json()["cancel_requested_at"] is not None
    job["revision"] = response.json()["revision"]
    response = query(client, job, QueryFake(QueryReceipt(status="NOT_ACCEPTED")))
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"
