"""GEN-14: financial evidence, transaction boundaries, and real database races."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_collaboration import create_project, register
from test_concurrency import require_postgres
from test_generation_budget import budget, priced, set_policy
from test_generation_delivery import Provider, deliver, event_for, retry_due
from test_generation_jobs import base, capture, capture_ok, payload
from test_generation_reconciliation import QueryFake, query, unknown

from arclight.application.generation_attempts import finish_attempt, request_retry, start_attempt
from arclight.application.generation_delivery import register_receipt
from arclight.application.generation_jobs import create_job
from arclight.application.generation_state import find_job, request_cancel
from arclight.core.errors import DomainError
from arclight.domain.generation import GenerationStatus as Status
from arclight.domain.generation_requests import GenerationCreate, GenerationResult
from arclight.persistence.generation_accounting import (
    GenerationBudgetSettlement as Settlement,
)
from arclight.persistence.models import AuditEvent, GenerationAttempt, GenerationJob
from arclight.providers.media_query import QueryReceipt
from arclight.providers.media_submission import SubmissionReceipt


def ledger(client, project_id):
    response = client.get(budget(project_id) + "/ledger")
    assert response.status_code == 200, response.text
    return response.json()


def usage(client, project_id):
    value = client.get(budget(project_id)).json()
    return tuple(Decimal(value[key]) for key in ("actual_cost", "pending_estimate"))


def invoice(client, project_id, row, amount="1.25", **changes):
    return client.post(
        budget(project_id) + f"/reservations/{row['id']}/settlement",
        json={
            "cost": amount,
            "confirmed": True,
            "evidence_reference": "billing/invoice-17",
            **changes,
        },
    )


def terminal(client, job, *, cost=None, status=Status.SUCCEEDED):
    with client.app.state.session_factory.begin() as db:
        attempt = start_attempt(db, UUID(job["id"]), job["revision"])
        return finish_attempt(
            db,
            UUID(job["id"]),
            attempt.id,
            status,
            result=GenerationResult(output_handles=["fake/image/1"])
            if status == Status.SUCCEEDED
            else None,
            error_code=None if status == Status.SUCCEEDED else "PROVIDER_FAILED",
            cost=cost,
        )


def test_creation_reserves_once_and_callback_settles_once(client, episode, monkeypatch):
    project_id = episode["project_id"]
    set_policy(client, project_id, "4")
    priced(monkeypatch)
    key = str(uuid4())
    created = capture(client, project_id, key=key)
    assert created.status_code == 201, created.text
    job = created.json()
    assert capture(client, project_id, key=key).json() == job
    assert usage(client, project_id) == (0, 2)
    assert len(ledger(client, project_id)) == 1
    receipt = SubmissionReceipt(
        status="SUCCEEDED",
        cost=Decimal("1.25"),
        result=GenerationResult(output_handles=["fake/image/1"]),
    )
    provider = Provider(receipt=receipt)
    event = event_for(client, job)
    assert deliver(client, job, provider, event) == "SUCCEEDED"
    assert deliver(client, job, provider, event) == "SUCCEEDED"
    with client.app.state.session_factory.begin() as db:
        attempt = db.scalar(
            select(GenerationAttempt).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
        register_receipt(db, UUID(job["id"]), attempt.id, receipt)
        assert db.scalar(select(func.count()).select_from(Settlement)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "budget.settled")
            )
            == 1
        )
    assert len(provider.requests) == 1
    assert usage(client, project_id) == (Decimal("1.25"), 0)
    row = ledger(client, project_id)[0]
    assert row["cost"] == "1.250000" and row["released"] == "0.750000"


def test_job_reservation_and_outbox_rollback_as_one_transaction(client, episode, monkeypatch):
    from arclight.application import generation_jobs

    project_id = episode["project_id"]
    set_policy(client, project_id)
    priced(monkeypatch)

    def fail(*args):
        raise DomainError("TEST_OUTBOX_FAILURE", "测试事务回滚", 409)

    monkeypatch.setattr(generation_jobs, "enqueue_dispatch", fail)
    assert capture(client, project_id).status_code == 409
    assert ledger(client, project_id) == []
    assert usage(client, project_id) == (0, 0)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0


@pytest.mark.parametrize(
    "cost,expected", [(None, (0, 2)), (Decimal("0"), (0, 0)), (Decimal("0.4"), (Decimal("0.4"), 0))]
)
def test_failed_paid_call_releases_only_known_unspent_budget(
    client, episode, monkeypatch, cost, expected
):
    project_id = episode["project_id"]
    set_policy(client, project_id)
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    terminal(client, job, cost=cost, status=Status.FAILED)
    assert usage(client, project_id) == expected
    row = ledger(client, project_id)[0]
    assert row["status"] == ("HELD" if cost is None else "SETTLED")


@pytest.mark.parametrize("before_submit", [True, False])
def test_cancel_intent_never_releases_a_submitted_reservation(
    client, episode, monkeypatch, before_submit
):
    project_id = episode["project_id"]
    set_policy(client, project_id)
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    if not before_submit:
        assert (
            deliver(
                client,
                job,
                Provider(receipt=SubmissionReceipt(status="SUBMITTED", request_id="accepted-17")),
            )
            == "SUBMITTED"
        )
    with client.app.state.session_factory.begin() as db:
        current = find_job(db, UUID(job["id"]), lock=True)
        request_cancel(db, current.id, current.revision, "service-owner")
    assert usage(client, project_id) == ((0, 0) if before_submit else (0, 2))
    assert ledger(client, project_id)[0]["status"] == ("SETTLED" if before_submit else "HELD")


@pytest.mark.parametrize("change", ["price", "target", "time"])
def test_pre_submission_failure_releases_without_calling_provider(
    client, episode, monkeypatch, change
):
    from datetime import timedelta

    from arclight.application import generation_leases
    from arclight.persistence.base import utcnow

    project_id = episode["project_id"]
    set_policy(client, project_id)
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    if change == "price":
        priced(monkeypatch, version="changed")
    elif change == "target":
        assert (
            client.patch(f"/api/v1/projects/{project_id}", json={"archived": True}).status_code
            == 200
        )
    else:
        future = utcnow() + timedelta(days=1)
        monkeypatch.setattr(generation_leases, "clock", lambda db: future)
    provider = Provider()
    assert deliver(client, job, provider) == "FAILED"
    assert provider.requests == [] and usage(client, project_id) == (0, 0)
    assert ledger(client, project_id)[0]["source"] == "PRE_SUBMISSION"


@pytest.mark.parametrize("outcome", ["NOT_FOUND", "UNKNOWN", "CANCELLED", "FAILED", "SUCCEEDED"])
def test_unknown_and_final_result_without_bill_remain_held(client, episode, monkeypatch, outcome):
    project_id = episode["project_id"]
    set_policy(client, project_id, "2")
    priced(monkeypatch)
    job, _ = unknown(client, project_id)
    row = ledger(client, project_id)[0]
    assert invoice(client, project_id, row).json()["error"]["code"] == "BUDGET_EXECUTION_UNRESOLVED"
    receipt = QueryReceipt(
        status=outcome,
        result=GenerationResult(output_handles=["image-17"]) if outcome == "SUCCEEDED" else None,
    )
    response = query(client, job, QueryFake(receipt))
    assert response.status_code == 200, response.text
    assert usage(client, project_id) == (0, 2)
    assert capture(client, project_id).json()["error"]["code"] == "BUDGET_EXCEEDED"
    assert ledger(client, project_id)[0]["status"] == "HELD"


def test_not_accepted_releases_old_attempt_and_reserves_retry_atomically(
    client, episode, monkeypatch
):
    project_id = episode["project_id"]
    set_policy(client, project_id, "2", task_limit="2")
    priced(monkeypatch)
    job, _ = unknown(client, project_id)
    response = query(client, job, QueryFake(QueryReceipt(status="NOT_ACCEPTED")))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "RETRY_WAIT"
    rows = ledger(client, project_id)
    assert len(rows) == 2
    assert {row["attempt_no"]: row["source"] for row in rows} == {1: "NOT_ACCEPTED", 2: "RETRY"}
    assert usage(client, project_id) == (0, 2)
    retry_due(client, job)
    receipt = SubmissionReceipt(
        status="SUCCEEDED",
        cost=Decimal("1.5"),
        result=GenerationResult(output_handles=["image-18"]),
    )
    assert deliver(client, job, Provider(receipt=receipt)) == "SUCCEEDED"
    assert usage(client, project_id) == (Decimal("1.5"), 0)


def test_manual_invoice_is_owner_evidence_idempotent_and_preserves_attempt(
    client, episode, monkeypatch
):
    project_id = episode["project_id"]
    set_policy(client, project_id, "2")
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    attempt = terminal(client, job)
    row = ledger(client, project_id)[0]
    response = invoice(client, project_id, row)
    assert response.status_code == 200, response.text
    assert invoice(client, project_id, row).json() == response.json()
    assert invoice(client, project_id, row, "1.2").status_code == 409
    assert usage(client, project_id) == (Decimal("1.25"), 0)
    with client.app.state.session_factory() as db:
        assert db.get(GenerationAttempt, attempt.id).cost is None
        assert db.scalar(select(func.count()).select_from(Settlement)) == 1


@pytest.mark.parametrize("cost", ["-1", "NaN", "Infinity", "0.0000001", "1000000000000"])
def test_invoice_rejects_invalid_costs(client, episode, cost):
    response = invoice(client, episode["project_id"], {"id": str(uuid4())}, cost)
    assert response.status_code == 422


def test_over_quote_actual_bill_is_recorded_and_blocks_new_paid_work(client, episode, monkeypatch):
    project_id = episode["project_id"]
    set_policy(client, project_id, "2")
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    terminal(client, job, cost=Decimal("3"))
    assert usage(client, project_id) == (3, 0)
    assert ledger(client, project_id)[0]["released"] == "-1.000000"
    assert capture(client, project_id).json()["error"]["code"] == "BUDGET_EXCEEDED"
    assert client.get(base(project_id) + f"/{job['id']}/attempts").status_code == 200


def test_retry_reservation_failure_does_not_queue_or_duplicate_spending(
    client, episode, monkeypatch
):
    project_id = episode["project_id"]
    set_policy(client, project_id, "2")
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    terminal(client, job, cost=Decimal("1"), status=Status.FAILED)
    with client.app.state.session_factory.begin() as db:
        current = find_job(db, UUID(job["id"]), lock=True)
        with pytest.raises(DomainError) as exc:
            request_retry(db, current.id, current.revision, "service-owner")
        assert exc.value.code == "BUDGET_EXCEEDED" and current.status == "FAILED"
    assert len(ledger(client, project_id)) == 1 and usage(client, project_id) == (1, 0)


def test_repeated_pre_submission_retry_keeps_separate_immutable_reservations(
    client, episode, monkeypatch
):
    project_id = episode["project_id"]
    set_policy(client, project_id, "4")
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    terminal(client, job, cost=Decimal("0"), status=Status.FAILED)
    with client.app.state.session_factory.begin() as db:
        current = find_job(db, UUID(job["id"]), lock=True)
        request_retry(db, current.id, current.revision, "service-owner")
    set_policy(client, project_id, "1")
    retry_due(client, job)
    provider = Provider()
    assert deliver(client, job, provider) == "FAILED" and provider.requests == []
    assert usage(client, project_id) == (0, 0)
    set_policy(client, project_id, "4")
    with client.app.state.session_factory.begin() as db:
        current = find_job(db, UUID(job["id"]), lock=True)
        request_retry(db, current.id, current.revision, "service-owner")
    rows = [row for row in ledger(client, project_id) if row["attempt_no"] == 2]
    assert len(rows) == 2
    assert {row["sequence"]: row["status"] for row in rows} == {1: "SETTLED", 2: "HELD"}
    retry_due(client, job)
    receipt = SubmissionReceipt(
        status="SUCCEEDED",
        cost=Decimal("1"),
        result=GenerationResult(output_handles=["fake/image/1"]),
    )
    assert deliver(client, job, Provider(receipt=receipt)) == "SUCCEEDED"
    assert usage(client, project_id) == (1, 0)


def test_cancel_supersedes_query_without_releasing_funds_until_new_evidence(
    client, episode, monkeypatch
):
    project_id = episode["project_id"]
    set_policy(client, project_id, "2")
    priced(monkeypatch)
    job, _ = unknown(client, project_id)

    def cancel():
        with client.app.state.session_factory.begin() as db:
            current = find_job(db, UUID(job["id"]), lock=True)
            request_cancel(db, current.id, current.revision, "service-owner")

    receipt = QueryReceipt(
        status="SUCCEEDED",
        cost=Decimal("1"),
        result=GenerationResult(output_handles=["fake/image/1"]),
    )
    response = query(client, job, QueryFake(receipt, action=cancel))
    assert response.status_code == 200, response.text
    assert usage(client, project_id) == (0, 2)
    job["revision"] = response.json()["revision"]
    response = query(client, job, QueryFake(receipt))
    assert response.status_code == 200, response.text
    assert usage(client, project_id) == (1, 0)


def test_ledger_pagination_permission_isolation_and_archived_reading(client, monkeypatch):
    owner = register(client)
    project_id = create_project(client)["id"]
    token = owner["access_token"]
    members = [
        (register(client, f"ledger-{role}@example.com"), role) for role in ("editor", "viewer")
    ]
    client.headers["Authorization"] = "Bearer " + token
    set_policy(client, project_id)
    priced(monkeypatch)
    for _ in range(2):
        terminal(client, capture_ok(client, project_id))
    rows = ledger(client, project_id)
    assert len(rows) == 2
    assert len(client.get(budget(project_id) + "/ledger?limit=1&offset=1").json()) == 1
    assert client.get(budget(project_id) + "/ledger?limit=1&offset=2").json() == []
    for member, role in members:
        client.headers["Authorization"] = "Bearer " + token
        assert (
            client.post(
                f"/api/v1/projects/{project_id}/members",
                json={"email": member["user"]["email"], "role": role},
            ).status_code
            == 201
        )
        client.headers["Authorization"] = "Bearer " + member["access_token"]
        assert len(ledger(client, project_id)) == 2
        assert invoice(client, project_id, rows[0]).status_code == 403
    client.headers["Authorization"] = "Bearer " + token
    other = create_project(client)["id"]
    assert invoice(client, other, rows[0]).status_code == 404
    assert invoice(client, project_id, rows[0], confirmed=False).status_code == 422
    assert (
        invoice(
            client, project_id, rows[0], evidence_reference="https://private/invoice"
        ).status_code
        == 422
    )
    assert (
        client.patch(f"/api/v1/projects/{project_id}", json={"archived": True}).status_code == 200
    )
    assert len(ledger(client, project_id)) == 2
    assert invoice(client, project_id, rows[0]).status_code == 409
    register(client, "ledger-stranger@example.com")
    assert client.get(budget(project_id) + "/ledger").status_code == 404


def test_accounting_history_database_protection_and_downgrade(client, episode, migrated):
    terminal(client, capture_ok(client, episode["project_id"]))
    for table in ("generation_budget_reservations", "generation_budget_settlements"):
        with client.app.state.session_factory.begin() as db:
            for sql in (f"UPDATE {table} SET created_at=created_at", f"DELETE FROM {table}"):
                with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), db.begin_nested():
                    db.execute(text(sql))
            sql = (
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"
                if db.get_bind().dialect.name == "sqlite"
                else f"TRUNCATE {table} CASCADE"
            )
            with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), db.begin_nested():
                db.execute(text(sql))
    with pytest.raises(RuntimeError, match="Generation history exists"):
        command.downgrade(migrated, "0037")
    command.check(migrated)


def test_postgres_duplicate_settlement_and_creation_race(client, episode, settings, monkeypatch):
    require_postgres(settings)
    project_id = episode["project_id"]
    set_policy(client, project_id, "3")
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    with client.app.state.session_factory.begin() as db:
        attempt = start_attempt(db, UUID(job["id"]), job["revision"])
    barrier = Barrier(3)
    receipt = SubmissionReceipt(
        status="SUCCEEDED",
        cost=Decimal("1"),
        result=GenerationResult(output_handles=["fake/image/1"]),
    )

    def work(index):
        barrier.wait(timeout=15)
        try:
            with client.app.state.session_factory.begin() as db:
                if index == 2:
                    create_job(
                        db,
                        UUID(project_id),
                        "service-owner",
                        str(uuid4()),
                        GenerationCreate.model_validate(payload()),
                    )
                    return "CREATED"
                register_receipt(db, UUID(job["id"]), attempt.id, receipt)
                return "SETTLED"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(work, range(3)))
    assert outcomes[:2] == ["SETTLED", "SETTLED"]
    assert outcomes[2] in {"CREATED", "BUDGET_EXCEEDED"}
    if outcomes[2] == "BUDGET_EXCEEDED":
        capture_ok(client, project_id)
    assert usage(client, project_id) == (1, 2)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Settlement)) == 1


def test_postgres_two_retries_compete_for_one_reservation(client, episode, settings, monkeypatch):
    require_postgres(settings)
    project_id = episode["project_id"]
    set_policy(client, project_id, "4")
    priced(monkeypatch)
    jobs = [capture_ok(client, project_id) for _ in range(2)]
    for job in jobs:
        terminal(client, job, cost=Decimal("0"), status=Status.FAILED)
    set_policy(client, project_id, "2")
    barrier = Barrier(2)

    def retry(job):
        barrier.wait(timeout=15)
        try:
            with client.app.state.session_factory.begin() as db:
                current = find_job(db, UUID(job["id"]), lock=True)
                request_retry(db, current.id, current.revision, "service-owner")
            return "RETRY_WAIT"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(retry, jobs))
    assert sorted(outcomes) == ["BUDGET_EXCEEDED", "RETRY_WAIT"]
    assert usage(client, project_id) == (0, 2)


def test_upgrade_backfills_evidence_without_rewriting_historical_attempts(settings):
    from pathlib import Path

    from alembic.config import Config

    from arclight.application.generation_accounting import costs
    from arclight.persistence.base import utcnow
    from arclight.persistence.generation import GenerationReconciliation
    from arclight.persistence.generation_accounting import GenerationBudgetReservation
    from arclight.persistence.generation_budget import GenerationBudgetQuote
    from arclight.persistence.models import DependencyManifest, Project
    from arclight.persistence.session import build_engine, session_factory

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "0037")
    engine = build_engine(settings)
    factory = session_factory(engine)
    cases = [
        ("queued", "CREATED", None, Decimal("2")),
        ("retry", "RETRY_WAIT", Decimal("0.5"), Decimal("2")),
        ("paid-unknown", "RESULT_UNKNOWN", None, Decimal("2")),
        ("paid-no-bill", "SUCCEEDED", None, Decimal("2")),
        ("billed", "SUCCEEDED", Decimal("1.25"), Decimal("2")),
        ("free", "RESULT_UNKNOWN", None, Decimal("0")),
        ("not-accepted", "RETRY_WAIT", None, Decimal("2")),
        ("cancelled-before-submit", "CANCELLED", None, Decimal("2")),
        ("before-quotes", "SUCCEEDED", Decimal("0.25"), None),
    ]
    with factory.begin() as db:
        project = Project(name="历史账目", owner_id="service-owner", budget_limit=100)
        db.add(project)
        db.flush()
        project_id = project.id
        identities = {}
        for name, status, cost, maximum in cases:
            manifest = DependencyManifest(
                project_id=project.id,
                creation_key=uuid4(),
                request_fingerprint="a" * 64,
                snapshot_json={"legacy": name},
                created_by="service-owner",
            )
            db.add(manifest)
            db.flush()
            count = 0 if name in {"queued", "cancelled-before-submit"} else 1
            job = GenerationJob(
                project_id=project.id,
                target_type="SHOT_IMAGE",
                task_type="IMAGE",
                provider="mock",
                model="mock-image",
                status=status,
                attempt_count=count,
                input_manifest_id=manifest.id,
                idempotency_key=name,
                request_fingerprint="a" * 64,
                created_by="service-owner",
            )
            db.add(job)
            db.flush()
            identities[name] = job.id
            if maximum is not None:
                db.add(
                    GenerationBudgetQuote(
                        job_id=job.id,
                        maximum_cost=maximum,
                        currency="USD",
                        pricing_version="historical",
                        candidate_count=1,
                    )
                )
            if count:
                pending = status == "RESULT_UNKNOWN"
                attempt = GenerationAttempt(
                    job_id=job.id,
                    attempt_no=1,
                    input_manifest_id=manifest.id,
                    request_fingerprint="a" * 64,
                    provider="mock",
                    model="mock-image",
                    status="RESULT_UNKNOWN" if pending else "FAILED",
                    input_snapshot_json={"legacy": name},
                    submitted_at=utcnow(),
                    finished_at=None if pending else utcnow(),
                    cost=cost,
                )
                db.add(attempt)
                db.flush()
                if name == "not-accepted":
                    db.add(
                        GenerationReconciliation(
                            job_id=job.id,
                            attempt_id=attempt.id,
                            claimed_revision=1,
                            source="PROVIDER",
                            actor="worker",
                            disposition="APPLIED",
                            receipt_json={"status": "NOT_ACCEPTED"},
                            finished_at=utcnow(),
                        )
                    )
    with engine.connect() as db:
        before = list(db.execute(select(GenerationAttempt.__table__)).mappings())
    command.upgrade(config, "head")
    command.check(config)
    with factory() as db:
        after = list(db.execute(select(GenerationAttempt.__table__)).mappings())
        assert sorted(before, key=lambda row: str(row["id"])) == sorted(
            after, key=lambda row: str(row["id"])
        )
        assert costs(db, project_id) == (Decimal("2"), Decimal("10"))
        values = list(
            db.execute(
                select(GenerationBudgetReservation, Settlement).outerjoin(
                    Settlement, Settlement.reservation_id == GenerationBudgetReservation.id
                )
            )
        )
        by_job = {}
        for reservation, settlement in values:
            by_job.setdefault(reservation.job_id, []).append((reservation, settlement))
        assert identities["cancelled-before-submit"] not in by_job
        assert identities["before-quotes"] not in by_job
        first = next(pair for pair in by_job[identities["not-accepted"]] if pair[0].attempt_no == 1)
        assert first[1].source == "NOT_ACCEPTED" and first[1].cost == 0
        assert by_job[identities["paid-no-bill"]][0][1] is None
    engine.dispose()
