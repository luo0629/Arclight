"""Generation proceeds independently of estimates and retired budget settings."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_collaboration import create_project, register
from test_concurrency import require_postgres
from test_generation_delivery import Provider, deliver
from test_generation_jobs import base, capture, capture_ok, payload

from arclight.application.generation_attempts import request_retry
from arclight.application.generation_jobs import create_job
from arclight.core.errors import DomainError
from arclight.domain.generation_budget import CostQuote
from arclight.domain.generation_requests import GenerationCreate
from arclight.domain.model_gateway import GatewayFailure, GatewayUsage
from arclight.persistence.generation_budget import (
    GenerationBudgetAdjustment,
    GenerationBudgetBatch,
    GenerationBudgetDecision,
    GenerationBudgetPolicy,
    GenerationBudgetQuote,
)
from arclight.persistence.models import GenerationJob, Project
from arclight.providers import media_pricing
from arclight.providers.media_submission import SubmissionReceipt


def budget(project_id):
    return f"/api/v1/projects/{project_id}/generation-costs"


def priced(monkeypatch, amount="2", version="test-price-v1"):
    monkeypatch.setattr(
        media_pricing,
        "quote",
        lambda spec, task: CostQuote(
            maximum_cost=Decimal(amount) * spec.batch_size,
            pricing_version=version,
        ),
    )


def test_estimate_is_advisory_and_replay_preserves_the_original_quote(client, episode, monkeypatch):
    project_id = episode["project_id"]
    priced(monkeypatch)
    estimate = client.post(budget(project_id) + "/estimate", json=payload()).json()
    assert set(estimate) == {"estimate"}
    assert Decimal(estimate["estimate"]["maximum_cost"]) == 2
    key = str(uuid4())
    job = capture_ok(client, project_id, key=key)
    priced(monkeypatch, "3", "new-price")
    assert capture(client, project_id, key=key).json() == job
    quote = client.get(budget(project_id) + "/jobs/" + job["id"]).json()["estimate"]
    assert quote["pricing_version"] == "test-price-v1" and Decimal(quote["maximum_cost"]) == 2
    provider = Provider()
    assert deliver(client, job, provider) == "SUCCEEDED"
    assert len(provider.requests) == 1


def test_unpriced_job_can_execute_and_billing_remains_unknown(
    client, episode, monkeypatch, migrated
):
    project_id = episode["project_id"]
    monkeypatch.setattr(media_pricing, "quote", lambda spec, task: None)
    assert client.post(budget(project_id) + "/estimate", json=payload()).json() == {
        "estimate": None
    }
    job = capture_ok(client, project_id)
    assert client.get(budget(project_id) + "/jobs/" + job["id"]).json() == {"estimate": None}
    assert deliver(client, job, Provider()) == "SUCCEEDED"
    records = client.get(budget(project_id) + "/ledger").json()
    assert len(records) == 1
    assert records[0]["reserved"] is None and records[0]["cost"] is None
    assert records[0]["status"] == "HELD"

    with pytest.raises(
        RuntimeError, match="Cannot downgrade without discarding execution or billing history"
    ):
        command.downgrade(migrated, "0071")
    command.check(migrated)
    assert client.get(budget(project_id) + "/ledger").json() == records


def test_historical_project_episode_task_and_candidate_caps_do_not_block_creation(
    client, episode, monkeypatch
):
    project_id = UUID(episode["project_id"])
    priced(monkeypatch)
    with client.app.state.session_factory.begin() as db:
        db.get(Project, project_id).budget_limit = Decimal("0.01")
        db.add(
            GenerationBudgetPolicy(
                project_id=project_id,
                task_limit=Decimal("0.01"),
                episode_limit=Decimal("0.01"),
                batch_limit=Decimal("0.01"),
                max_candidates_per_target=1,
            )
        )
    body = payload(episode_id=episode["id"])
    body["spec"]["batch_size"] = 2
    jobs = [capture_ok(client, str(project_id), body) for _ in range(3)]
    assert len({job["id"] for job in jobs}) == 3
    with client.app.state.session_factory() as db:
        assert db.get(Project, project_id).budget_limit == Decimal("0.01")


def test_retired_budget_routes_and_request_fields_are_rejected(client, episode):
    project = f"/api/v1/projects/{episode['project_id']}"
    for suffix in ("", "/estimate", "/batches", "/adjustments", "/ledger"):
        assert (
            client.request(
                "GET" if not suffix or suffix == "/ledger" else "POST", project + "/budget" + suffix
            ).status_code
            == 404
        )
    assert client.patch(project, json={"budget_limit": "1"}).status_code == 422
    assert capture(client, episode["project_id"], payload(batch_id=str(uuid4()))).status_code == 422
    assert "budget_limit" not in client.get(project).json()


def test_unknown_result_keeps_estimated_cost_without_preventing_unrelated_work(
    client, episode, monkeypatch
):
    project_id = episode["project_id"]
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    provider = Provider(
        receipt=SubmissionReceipt(status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN")
    )
    assert deliver(client, job, provider) == "RESULT_UNKNOWN"
    assert capture(client, project_id).status_code == 201
    records = client.get(budget(project_id) + "/ledger").json()
    original = next(row for row in records if row["job_id"] == job["id"])
    assert original["status"] == "HELD" and Decimal(original["reserved"]) == 2


@pytest.mark.parametrize("accepted", [False, True])
def test_media_balance_refusal_is_distinct_and_only_nonacceptance_allows_resume(
    client, episode, monkeypatch, accepted
):
    project_id = episode["project_id"]
    monkeypatch.setattr(media_pricing, "quote", lambda spec, task: None)
    job = capture_ok(client, project_id)
    factory = client.app.state.session_factory
    job_id = UUID(job["id"])

    def reject(_):
        raise GatewayFailure(
            "PROVIDER_INSUFFICIENT_BALANCE",
            not_accepted=not accepted,
            completed=accepted,
            usage=GatewayUsage(images=1) if accepted else None,
        )

    provider = Provider(action=reject)
    assert deliver(client, job, provider) == "FAILED"
    assert len(provider.requests) == 1
    events = client.get(base(project_id) + f"/{job['id']}/attempts").json()
    assert events[0]["error_code"] == "ACCOUNT_BALANCE_INSUFFICIENT"
    original = client.get(budget(project_id) + "/ledger").json()[0]
    if accepted:
        assert original["status"] == "HELD" and original["cost"] is None
        with factory.begin() as db, pytest.raises(DomainError, match="原调用已受理"):
            run = db.get(GenerationJob, job_id)
            request_retry(db, job_id, run.revision, "service-owner")
        return

    assert original["status"] == "SETTLED" and Decimal(original["cost"]) == 0
    for _ in range(3):
        with factory.begin() as db:
            run = db.get(GenerationJob, job_id)
            assert request_retry(db, job_id, run.revision, "service-owner").status == "RETRY_WAIT"
        assert deliver(client, job, provider) == "FAILED"
    with factory.begin() as db:
        run = db.get(GenerationJob, job_id)
        assert request_retry(db, job_id, run.revision, "service-owner").status == "RETRY_WAIT"
    assert deliver(client, job, Provider()) == "SUCCEEDED"
    attempts = client.get(base(project_id) + f"/{job['id']}/attempts").json()
    assert len(attempts) == 5
    assert len(provider.requests) == 4
    assert all(r.snapshot == provider.requests[0].snapshot for r in provider.requests)


def test_postgres_parallel_creation_records_both_reservations(
    client, episode, settings, monkeypatch
):
    require_postgres(settings)
    project_id = UUID(episode["project_id"])
    priced(monkeypatch)
    barrier = Barrier(2)

    def create_one(_):
        barrier.wait(timeout=15)
        with client.app.state.session_factory.begin() as db:
            return create_job(
                db,
                project_id,
                "service-owner",
                str(uuid4()),
                GenerationCreate.model_validate(payload()),
            ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(create_one, range(2)))
    assert len(set(jobs)) == 2
    assert len(client.get(budget(project_id) + "/ledger").json()) == 2


@pytest.mark.usefixtures("legacy_execution_deadlines")
def test_quote_history_and_protective_downgrade(client, episode, migrated):
    job = capture_ok(client, episode["project_id"])
    with client.app.state.session_factory() as db:
        row = db.scalar(
            select(GenerationBudgetQuote).where(GenerationBudgetQuote.job_id == UUID(job["id"]))
        )
        quote_id = row.id
    for statement in (
        "UPDATE generation_budget_quotes SET maximum_cost=100 WHERE id=:id",
        "DELETE FROM generation_budget_quotes WHERE id=:id",
    ):
        with (
            client.app.state.session_factory.begin() as db,
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            db.begin_nested(),
        ):
            identity = quote_id.hex if db.get_bind().dialect.name == "sqlite" else quote_id
            db.execute(text(statement), {"id": identity})
    with pytest.raises(RuntimeError, match="Generation history exists"):
        command.downgrade(migrated, "0036")


def test_estimate_and_ledger_permissions_preserve_project_isolation(client):
    owner = register(client)
    project_id = create_project(client)["id"]
    owner_token = owner["access_token"]
    editor = register(client, "cost-editor@example.com")
    viewer = register(client, "cost-viewer@example.com")
    client.headers["Authorization"] = "Bearer " + owner_token
    job = capture_ok(client, project_id)
    for member, role in ((editor, "editor"), (viewer, "viewer")):
        response = client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"email": member["user"]["email"], "role": role},
        )
        assert response.status_code == 201, response.text
    for member, status in [(editor, 200), (viewer, 403)]:
        client.headers["Authorization"] = "Bearer " + member["access_token"]
        assert client.get(budget(project_id) + "/ledger").status_code == 200
        assert client.get(budget(project_id) + "/jobs/" + job["id"]).status_code == 200
        assert client.post(budget(project_id) + "/estimate", json=payload()).status_code == status
    register(client, "cost-stranger@example.com")
    assert client.get(budget(project_id) + "/ledger").status_code == 404


def test_archived_project_retains_ledger(client, episode):
    project_id = episode["project_id"]
    job = capture_ok(client, project_id)
    assert (
        client.patch(f"/api/v1/projects/{project_id}", json={"archived": True}).status_code == 200
    )
    assert client.get(budget(project_id) + "/ledger").status_code == 200
    assert client.get(base(project_id) + "/" + job["id"]).status_code == 200
    assert client.post(budget(project_id) + "/estimate", json=payload()).status_code == 409


def test_historical_budget_facts_reject_replacement_and_truncation(client, episode):
    project_id = UUID(episode["project_id"])
    capture_ok(client, str(project_id))
    with client.app.state.session_factory.begin() as db:
        db.add(
            GenerationBudgetBatch(
                project_id=project_id,
                creation_key=uuid4(),
                budget_limit=Decimal(1),
                created_by="service-owner",
            )
        )
        adjustment = GenerationBudgetAdjustment(
            project_id=project_id,
            creation_key=uuid4(),
            requested_limit=Decimal(5),
            reason="原额度申请",
            created_by="service-owner",
        )
        db.add(adjustment)
        db.flush()
        db.add(
            GenerationBudgetDecision(
                adjustment_id=adjustment.id,
                approved=False,
                reason="原决定",
                decided_by="service-owner",
                policy_revision=0,
            )
        )
    for table in (
        "generation_budget_quotes",
        "generation_budget_batches",
        "generation_budget_adjustments",
        "generation_budget_decisions",
    ):
        with client.app.state.session_factory.begin() as db:
            statement = (
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"
                if db.get_bind().dialect.name == "sqlite"
                else f"TRUNCATE {table} CASCADE"
            )
            with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), db.begin_nested():
                db.execute(text(statement))
