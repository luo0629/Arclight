"""GEN-15: real ledger projections, missing bills, grouping and access boundaries."""

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import event, func, select
from test_collaboration import create_project, register
from test_generation_accounting import invoice, ledger, terminal
from test_generation_budget import priced, set_policy
from test_generation_delivery import Provider, deliver, event_for, retry_due
from test_generation_jobs import capture_ok, payload
from test_model_gateway import request, route

from arclight.application.gateway_accounting import reconcile
from arclight.application.generation_attempts import request_retry
from arclight.application.generation_state import find_job, request_cancel
from arclight.application.model_gateway import invoke
from arclight.domain.generation import GenerationStatus
from arclight.domain.generation_requests import GenerationResult
from arclight.domain.model_gateway import GatewayPolicy, GatewayUsage
from arclight.persistence.generation import GenerationAttempt, GenerationJob
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import WorkflowRun
from arclight.providers.media_submission import SubmissionReceipt


def url(episode):
    return f"/api/v1/projects/{episode['project_id']}/costs"


def report(client, episode, **query):
    response = client.get(url(episode), params=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_empty_costs_do_not_invent_a_bill_and_all_dimensions_are_bounded(client, episode):
    for dimension in ("project", "episode", "task", "model"):
        result = report(client, episode, group_by=dimension)
        summary = result["summary"]
        assert summary["entry_count"] == 0
        assert summary["actual_cost"] is None and summary["estimated_cost"] is None
        assert summary["pending_estimate"] == "0.000000"
        assert result["groups"] == [] and result["total_groups"] == 0
        assert result["currency"] == "USD" and result["generated_at"].endswith("Z")
    assert client.get(url(episode) + "/entries").json()["items"] == []
    for query in ({"group_by": "secret"}, {"limit": 101}, {"offset": -1}, {"model": ""}):
        assert client.get(url(episode), params=query).status_code == 422


def test_media_estimate_known_bill_and_manual_invoice_update_all_four_dimensions(
    client, episode, monkeypatch
):
    set_policy(client, episode["project_id"], "100")
    priced(monkeypatch)
    billed = capture_ok(client, episode["project_id"], payload(episode_id=episode["id"]))
    terminal(client, billed, cost=Decimal("1.123456"))
    unknown = capture_ok(client, episode["project_id"])
    terminal(client, unknown)
    for dimension in ("project", "episode", "task", "model"):
        result = report(client, episode, group_by=dimension)
        summary = result["summary"]
        assert summary["entry_count"] == 2 and summary["confirmed_count"] == 1
        assert summary["actual_cost"] == "1.123456"
        assert summary["estimated_cost"] == "4.000000"
        assert summary["pending_estimate"] == "2.000000"
        assert summary["projected_cost"] == "3.123456"
        assert summary["cost_is_estimate"] and summary["estimated_count"] == 1
        assert sum(group["entry_count"] for group in result["groups"]) == 2
    scoped = report(client, episode, episode_id=episode["id"])
    assert scoped["summary"]["actual_cost"] == "1.123456"
    assert scoped["summary"]["pending_estimate"] == "0.000000"
    rows = ledger(client, episode["project_id"])
    reservation = next(row for row in rows if row["job_id"] == unknown["id"])
    assert invoice(client, episode["project_id"], reservation, "0.765432").status_code == 200
    final = report(client, episode)["summary"]
    assert final["actual_cost"] == "1.888888" and not final["cost_is_estimate"]
    detail = client.get(url(episode) + "/entries", params={"task_id": unknown["id"]}).json()
    assert detail["total"] == 1
    row = detail["items"][0]
    assert row["billing_source"] == "MANUAL" and row["actual_cost"] == "0.765432"
    assert row["attempt_id"] and row["input_manifest_id"] and row["attempt_no"] == 1
    assert "billing/invoice" not in str(detail) and "雨夜" not in str(detail)


def test_gateway_missing_receipt_and_reconciliation_are_counted_once(client, episode):
    set_policy(client, episode["project_id"], "100")
    selected = route()
    req = request(episode, selected, task="script")
    assert (
        invoke(
            client.app.state.session_factory, req, selected, lambda: "ok", policy=GatewayPolicy()
        )
        == "ok"
    )
    before = report(client, episode)["summary"]
    assert before["actual_cost"] is None
    assert before["estimated_count"] == 1 and before["pending_estimate"] == "2.000000"
    with client.app.state.session_factory.begin() as db:
        call = db.scalar(select(GatewayCall).where(GatewayCall.operation_key == req.operation_key))
        reconcile(
            db,
            call,
            "service-owner",
            "SUCCEEDED",
            GatewayUsage(actual_cost=Decimal("0.3")),
            "private-invoice",
        )
    result = report(client, episode, group_by="model")
    assert result["summary"]["entry_count"] == 1
    assert result["summary"]["actual_cost"] == "0.300000"
    assert result["groups"][0]["model"] == "test-model"
    assert "private-invoice" not in str(result)


def test_unpriced_text_keeps_unknown_costs_and_does_not_break_later_budget_checks(client, episode):
    started = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        json={"story": "quiet forest"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    workflow_id = UUID(started.json()["run_id"])
    selected = route(maximum_cost=None)
    invoke(
        client.app.state.session_factory,
        request(episode, selected, task="script", workflow_id=workflow_id),
        selected,
        lambda: "ok",
        policy=GatewayPolicy(),
        usage=lambda _: GatewayUsage(input_tokens=7, output_tokens=9),
    )
    for dimension in ("project", "episode", "task", "model"):
        result = report(client, episode, group_by=dimension)["summary"]
        assert result["entry_count"] == 1 and result["unpriced_count"] == 1
        assert result["actual_cost"] is None and result["estimated_cost"] is None
        assert result["projected_cost"] is None and result["cost_is_estimate"]
    detail = client.get(url(episode) + "/entries").json()["items"][0]
    assert detail["estimated_cost"] is None and detail["actual_cost"] is None
    assert detail["pending_estimate"] == "0.000000"
    set_policy(client, episode["project_id"], "5", task_limit="4")
    priced_route = route()
    invoke(
        client.app.state.session_factory,
        request(episode, priced_route, task="script", workflow_id=workflow_id),
        priced_route,
        lambda: "ok",
        policy=GatewayPolicy(),
    )
    assert report(client, episode)["summary"]["unpriced_count"] == 1


def test_media_gateway_and_duplicate_delivery_never_double_bill(client, episode, monkeypatch):
    priced(monkeypatch)
    set_policy(client, episode["project_id"], "20")
    job = capture_ok(client, episode["project_id"])
    provider = Provider(
        receipt=SubmissionReceipt(
            status="SUCCEEDED",
            cost=Decimal("1.000001"),
            result=GenerationResult(output_handles=["fake/image/1"]),
        )
    )
    dispatch = event_for(client, job)
    assert deliver(client, job, provider, dispatch) == "SUCCEEDED"
    assert deliver(client, job, provider, dispatch) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GatewayCall)) == 1
    summary = report(client, episode)["summary"]
    assert summary["entry_count"] == 1 and summary["actual_cost"] == "1.000001"
    assert summary["projected_cost"] == "1.000001"
    assert len(provider.requests) == 1


def test_retries_count_each_attempt_once_even_after_repeated_pre_submission_reservation(
    client, episode, monkeypatch
):
    project_id = episode["project_id"]
    priced(monkeypatch)
    set_policy(client, project_id, "4")
    job = capture_ok(client, project_id)
    terminal(client, job, cost=Decimal("0"), status=GenerationStatus.FAILED)
    with client.app.state.session_factory.begin() as db:
        current = find_job(db, UUID(job["id"]), lock=True)
        request_retry(db, current.id, current.revision, "service-owner")
    set_policy(client, project_id, "1")
    retry_due(client, job)
    assert deliver(client, job, Provider()) == "FAILED"
    set_policy(client, project_id, "4")
    with client.app.state.session_factory.begin() as db:
        current = find_job(db, UUID(job["id"]), lock=True)
        request_retry(db, current.id, current.revision, "service-owner")
    assert len(ledger(client, project_id)) == 3
    summary = report(client, episode)["summary"]
    assert summary["entry_count"] == 2
    assert summary["estimated_cost"] == "4.000000"
    assert summary["actual_cost"] == "0.000000" and summary["pending_estimate"] == "2.000000"


def test_historical_missing_quote_is_unknown_and_known_zero_stays_exact(client, episode):
    # These deliberately pre-ledger records model historical imports. Current
    # creation/delivery still goes through the real services in the other tests.
    template = capture_ok(client, episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        original = db.get(GenerationJob, UUID(template["id"]))
        for number, amount in enumerate((None, Decimal("0"), Decimal("0.123456"))):
            values = {
                column.name: getattr(original, column.name)
                for column in GenerationJob.__table__.columns
            }
            values.update(id=uuid4(), idempotency_key=str(uuid4()), attempt_count=1)
            historical = GenerationJob(**values)
            db.add(historical)
            db.flush()
            db.add(
                GenerationAttempt(
                    job_id=historical.id,
                    attempt_no=1,
                    input_manifest_id=original.input_manifest_id,
                    request_fingerprint=original.request_fingerprint,
                    provider="historical",
                    model=f"old-{number}",
                    status="SUCCEEDED",
                    input_snapshot_json={},
                    cost=amount,
                    submitted_at=original.created_at,
                    finished_at=original.created_at,
                )
            )
    result = report(client, episode, provider="historical")["summary"]
    assert result["entry_count"] == 3 and result["confirmed_count"] == 2
    assert result["actual_cost"] == "0.123456" and result["estimated_cost"] is None
    assert result["unpriced_count"] == 1 and result["projected_cost"] is None
    assert report(client, episode, model="old-0")["summary"]["actual_cost"] is None
    exact_zero = report(client, episode, model="old-1")["summary"]
    assert exact_zero["actual_cost"] == "0.000000" and not exact_zero["cost_is_estimate"]


def test_cancelled_unknown_and_over_quote_bills_keep_their_distinct_costs(
    client, episode, monkeypatch
):
    project_id = episode["project_id"]
    set_policy(client, project_id, "100")
    priced(monkeypatch)
    cancelled = capture_ok(client, project_id)
    with client.app.state.session_factory.begin() as db:
        current = find_job(db, UUID(cancelled["id"]), lock=True)
        request_cancel(db, current.id, current.revision, "service-owner")
    unresolved = capture_ok(client, project_id, payload(episode_id=episode["id"]))
    assert (
        deliver(
            client,
            unresolved,
            Provider(
                receipt=SubmissionReceipt(status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN")
            ),
        )
        == "RESULT_UNKNOWN"
    )
    over_quote = capture_ok(client, project_id)
    terminal(client, over_quote, cost=Decimal("3"))
    summary = report(client, episode)["summary"]
    assert summary["actual_cost"] == "3.000000"
    assert summary["pending_estimate"] == "2.000000"
    assert summary["projected_cost"] == "5.000000"
    entries = client.get(url(episode) + "/entries").json()["items"]
    by_task = {entry["task_id"]: entry for entry in entries}
    assert by_task[cancelled["id"]]["actual_cost"] == "0.000000"
    assert not by_task[cancelled["id"]]["cost_is_estimate"]
    assert by_task[unresolved["id"]]["actual_cost"] is None
    assert by_task[unresolved["id"]]["status"] == "RESULT_UNKNOWN"
    assert by_task[over_quote["id"]]["actual_cost"] == "3.000000"
    scoped = report(
        client, episode, episode_id=episode["id"], task_id=unresolved["id"], provider="mock"
    )
    assert scoped["summary"]["entry_count"] == 1
    assert scoped["summary"]["actual_cost"] is None


def test_unassigned_episode_filter_is_separate_from_a_specific_episode(client, episode):
    capture_ok(client, episode["project_id"], payload(episode_id=episode["id"]))
    job = capture_ok(client, episode["project_id"])
    result = report(client, episode, unassigned_episode=True)
    assert result["summary"]["entry_count"] == 1
    entries = client.get(url(episode) + "/entries", params={"unassigned_episode": True}).json()
    assert entries["total"] == 1 and entries["items"][0]["task_id"] == job["id"]
    assert (
        client.get(
            url(episode), params={"unassigned_episode": True, "episode_id": episode["id"]}
        ).status_code
        == 422
    )


def test_one_workflow_with_different_model_operations_is_one_task_group(client, episode):
    from test_api_workflow import start

    started = start(client, episode).json()
    run_id = UUID(started["run_id"])
    set_policy(client, episode["project_id"], "20")
    selected = route(
        capabilities=route().capabilities.model_copy(
            update={"tasks": frozenset({"script", "storyboard"})}
        )
    )
    for task in ("script", "storyboard"):
        req = request(episode, selected, task=task, workflow_id=run_id)
        invoke(
            client.app.state.session_factory,
            req,
            selected,
            lambda: "ok",
            policy=GatewayPolicy(),
            usage=lambda _: GatewayUsage(actual_cost=Decimal("0.1")),
        )
    result = report(client, episode, group_by="task")
    assert result["total_groups"] == 1 and result["groups"][0]["task_id"] == str(run_id)
    assert result["groups"][0]["entry_count"] == 2
    assert result["groups"][0]["actual_cost"] == "0.200000"
    with client.app.state.session_factory() as db:
        trace_id = db.get(WorkflowRun, run_id).trace_id
    entries = client.get(url(episode) + "/entries").json()["items"]
    assert len(entries) == 2 and {entry["trace_id"] for entry in entries} == {trace_id}


def test_pagination_does_not_change_totals_and_reads_do_not_write(client, episode, monkeypatch):
    set_policy(client, episode["project_id"], "100")
    priced(monkeypatch)
    jobs = [capture_ok(client, episode["project_id"]) for _ in range(3)]
    statements = []

    def capture_sql(connection, cursor, statement, parameters, context, many):
        statements.append(statement.lstrip().split()[0].upper())

    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        pages = [
            report(client, episode, group_by="task", limit=1, offset=index) for index in range(4)
        ]
        client.get(url(episode) + "/entries?limit=1&offset=1")
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
    assert not set(statements) & {"INSERT", "UPDATE", "DELETE"}
    assert {page["summary"]["pending_estimate"] for page in pages} == {"6.000000"}
    assert {page["total_groups"] for page in pages} == {3}
    assert {page["groups"][0]["task_id"] for page in pages[:3]} == {job["id"] for job in jobs}
    assert pages[-1]["groups"] == []
    assert report(client, episode, model="missing")["summary"]["entry_count"] == 0


def test_foreign_projects_and_filters_are_rejected_and_viewer_can_read(client):
    owner = register(client)
    project = create_project(client)
    viewer = register(client, "viewer-costs@example.com")
    assert client.get(f"/api/v1/projects/{project['id']}/costs").status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    added = client.post(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": "viewer-costs@example.com", "role": "viewer"},
    )
    assert added.status_code == 201, added.text
    client.headers["Authorization"] = "Bearer " + viewer["access_token"]
    for suffix in ("", "/entries"):
        endpoint = f"/api/v1/projects/{project['id']}/costs" + suffix
        assert client.get(endpoint).status_code == 200
        assert client.get(endpoint, params={"episode_id": str(uuid4())}).status_code == 404
        assert client.get(endpoint, params={"task_id": str(uuid4())}).status_code == 404
