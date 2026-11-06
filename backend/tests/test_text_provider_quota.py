"""Text may use supplier credit directly; rejection and billing facts stay precise."""

import json
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from alembic import command
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_api_workflow import advance, review, start
from test_costs import report
from test_generation_budget import set_policy
from test_model_gateway import api, request, route

from arclight.agents.contracts import ScriptContent
from arclight.application.gateway_accounting import reconcile
from arclight.application.model_gateway import invoke
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayPolicy, GatewayUsage
from arclight.domain.model_suppliers import supplier_by_id
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import WorkflowRun
from arclight.providers.text import MockTextProvider


@pytest.fixture
def text_service(settings, monkeypatch):
    supplier = supplier_by_id("deepseek")
    settings.provider = "openai_compatible"
    settings.llm_base_url = supplier.base_url
    settings.llm_model = supplier.model
    settings.llm_supplier = supplier.id
    settings.llm_response_format = supplier.response_format
    settings.llm_api_key = SecretStr("private-text-test-key")
    settings.model_gateway_policy = GatewayPolicy()

    def respond(handler):
        monkeypatch.setattr(
            "arclight.providers.text.PublicHTTPTransport",
            lambda *args, **kwargs: httpx.MockTransport(handler),
        )

    return respond


def script_response(req):
    body = json.loads(req.content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    content = MockTextProvider().script("虚构的森林故事", {}, 30000)
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content.model_dump_json()}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 17},
        },
    )


def test_no_quote_or_project_cap_required_and_missing_money_stays_unknown(
    client, episode, settings, text_service, migrated
):
    text_service(script_response)
    run_id = start(client, episode).json()["run_id"]
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    rows = client.get(api(episode) + "/calls").json()
    assert len(rows) == 1 and rows[0]["maximum_cost"] is None
    assert rows[0]["usage"]["actual_cost"] is None
    assert rows[0]["usage"]["input_tokens"] == 11
    summary = report(client, episode)["summary"]
    assert summary["unpriced_count"] == 1
    assert summary["actual_cost"] is None and summary["estimated_cost"] is None
    assert summary["projected_cost"] is None
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, UUID(run_id)).agent_state_json = None
    # The newest immutable journal must reject the whole destructive downgrade.
    # The explicit pre-0048 test below still exercises the original pricing guard.
    with pytest.raises(RuntimeError, match="Human review history exists"):
        command.downgrade(migrated, "0041")
    # The rejected downgrade rolls back. The same unknown quote and guards survive.
    with client.app.state.session_factory() as db:
        call = db.scalar(select(GatewayCall).where(GatewayCall.workflow_id == UUID(run_id)))
        assert call.maximum_cost is None
        identity = call.id
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(
            text("UPDATE model_gateway_calls SET maximum_cost=0 WHERE id=:id"),
            {"id": identity.hex if db.bind.dialect.name == "sqlite" else identity},
        )


def test_pre_stage_schema_still_protects_unpriced_text_history(
    settings, migrated, monkeypatch, text_service
):
    from fastapi.testclient import TestClient
    from legacy_outbox import OutboxEvent as LegacyOutboxEvent
    from test_collaboration import create_project

    from arclight.application import commands, production_events
    from arclight.main import create_app

    # Seed the actual schema immediately before stage notifications existed.
    # These patches model only the old writer; current-schema coverage above
    # always writes real production events and retains their earlier guard.
    command.downgrade(migrated, "0047")
    monkeypatch.setattr(commands, "OutboxEvent", LegacyOutboxEvent)
    monkeypatch.setattr(production_events, "enqueue_text", lambda db, run: None)
    from arclight.application import text_reviews

    # Explicit pre-0048 schema has no unified review journal either.
    monkeypatch.setattr(text_reviews, "pause_for_run", lambda db, run: None)
    text_service(script_response)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer test-token"
        project = create_project(client)
        response = client.post(
            f"/api/v1/projects/{project['id']}/episodes",
            json={
                "episode_no": 1,
                "title": "旧版费用记录",
                "target_duration_ms": 30000,
            },
        )
        assert response.status_code == 201
        run_id = start(client, response.json()).json()["run_id"]
        assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
        with client.app.state.session_factory.begin() as db:
            db.get(WorkflowRun, UUID(run_id)).agent_state_json = None
        with pytest.raises(RuntimeError, match="Unpriced text history"):
            command.downgrade(migrated, "0041")
        with client.app.state.session_factory() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0047"
            call = db.scalar(select(GatewayCall).where(GatewayCall.workflow_id == UUID(run_id)))
            assert call.maximum_cost is None


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (402, {"error": {"message": "private-text-test-key"}}, "GATEWAY_QUOTA_EXHAUSTED"),
        (429, {"error": {"code": "insufficient_quota"}}, "GATEWAY_QUOTA_EXHAUSTED"),
        (429, {"error": {"type": "insufficient_quota"}}, "GATEWAY_QUOTA_EXHAUSTED"),
        (400, {"error": {"code": "1113"}}, "GATEWAY_QUOTA_EXHAUSTED"),
        (429, {"error": {"code": "rate_limit_exceeded"}}, "GATEWAY_PROVIDER_RATE_LIMITED"),
        (429, {"error": {"message": "insufficient_quota"}}, "GATEWAY_PROVIDER_RATE_LIMITED"),
        (
            401,
            {"error": {"message": "private-text-test-key"}},
            "GATEWAY_PROVIDER_AUTHENTICATION_FAILED",
        ),
        (403, {"error": {"code": "forbidden"}}, "GATEWAY_PROVIDER_ACCESS_DENIED"),
        (503, {"error": {"code": "insufficient_quota"}}, "GATEWAY_RECONCILIATION_REQUIRED"),
        (500, {}, "GATEWAY_RECONCILIATION_REQUIRED"),
    ],
)
def test_provider_rejections_are_distinct_private_and_never_retried_automatically(
    client, episode, settings, text_service, caplog, status, body, expected
):
    requests = []

    def failure(req):
        requests.append(req)
        return httpx.Response(status, json=body)

    text_service(failure)
    settings.model_gateway_policy = GatewayPolicy(max_attempts=3)
    run_id = start(client, episode).json()["run_id"]
    state = advance(client, settings, run_id)
    assert state["status"] == "FAILED" and state["error_code"] == expected
    assert len(requests) == 1
    rows = client.get(api(episode) + "/calls").json()
    assert len(rows) == 1
    assert rows[0]["status"] == ("RESULT_UNKNOWN" if status >= 500 else "NOT_ACCEPTED")
    if status >= 500:
        assert rows[0]["usage"]["actual_cost"] is None
    else:
        assert Decimal(rows[0]["usage"]["actual_cost"]) == 0
    assert "private-text-test-key" not in str(rows) + str(state) + caplog.text
    if status >= 500:
        assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
        repeated = advance(client, settings, run_id)
        assert repeated["error_code"] == "GATEWAY_RECONCILIATION_REQUIRED"
        assert len(requests) == 1


def test_quota_rejection_can_recover_explicitly_with_the_same_input(
    client, episode, settings, text_service
):
    text_service(lambda req: httpx.Response(402, json={"error": {"type": "insufficient_balance"}}))
    run_id = start(client, episode).json()["run_id"]
    assert advance(client, settings, run_id)["error_code"] == "GATEWAY_QUOTA_EXHAUSTED"
    text_service(script_response)
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
    rows = client.get(api(episode) + "/calls").json()
    assert [row["status"] for row in rows] == ["NOT_ACCEPTED", "SUCCEEDED"]
    assert rows[0]["input_sha256"] == rows[1]["input_sha256"]


def test_enabling_a_cap_requires_resolving_unpriced_text_history(
    client, episode, settings, text_service
):
    text_service(script_response)
    run_id = start(client, episode).json()["run_id"]
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
    set_policy(client, episode["project_id"], "10")
    selected = route(maximum_cost=Decimal("1"))
    current = request(episode, selected)
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            current,
            selected,
            lambda: pytest.fail("unpriced history cannot be treated as zero"),
            policy=GatewayPolicy(),
        )
    assert caught.value.code == "BUDGET_UNPRICED_HISTORY"
    with client.app.state.session_factory.begin() as db:
        call = db.scalar(select(GatewayCall).where(GatewayCall.workflow_id == UUID(run_id)))
        reconcile(
            db,
            call,
            "service-owner",
            "SUCCEEDED",
            GatewayUsage(actual_cost=Decimal("1")),
            "Supplier invoice confirmed one USD",
        )
    assert (
        invoke(
            client.app.state.session_factory,
            current,
            selected,
            lambda: "ok",
            policy=GatewayPolicy(),
        )
        == "ok"
    )


def test_media_cannot_use_the_optional_text_quote_path(client, episode):
    selected = route(maximum_cost=None)
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            request(episode, selected),
            selected,
            lambda: pytest.fail("media still requires a quote"),
            policy=GatewayPolicy(),
        )
    assert caught.value.code == "BUDGET_QUOTE_UNAVAILABLE"


def test_quote_migration_preserves_existing_calls_receipts_and_guards(client, episode, migrated):
    selected = route(maximum_cost=Decimal("0"))
    invoke(
        client.app.state.session_factory,
        request(episode, selected),
        selected,
        lambda: "result",
        policy=GatewayPolicy(),
        usage=lambda _: GatewayUsage(actual_cost=Decimal("0")),
    )
    original = client.get(api(episode) + "/calls").json()
    command.downgrade(migrated, "0041")
    assert client.get(api(episode) + "/calls").json() == original
    command.upgrade(migrated, "head")
    assert client.get(api(episode) + "/calls").json() == original
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(text("DELETE FROM model_gateway_calls"))


def test_text_adapter_dual_review_and_storyboard_quota_recovery(
    client, episode, settings, text_service
):
    received = []
    reject_board = True

    def respond(req):
        envelope = json.loads(json.loads(req.content)["messages"][1]["content"])
        data = envelope.get("task_input", envelope)
        received.append(data)
        if "script" not in data:
            return script_response(req)
        if reject_board:
            return httpx.Response(402, json={"error": {"type": "insufficient_balance"}})
        content = MockTextProvider().storyboard(
            ScriptContent.model_validate(data["script"]), data["target_duration_ms"]
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content.model_dump_json()}}]}
        )

    text_service(respond)
    run_id = start(client, episode).json()["run_id"]
    script = advance(client, settings, run_id)
    assert script["status"] == "WAITING_REVIEW"
    assert script["pending_review"]["resource_type"] == "script"
    assert review(client, script).status_code == 202
    failed = advance(client, settings, run_id)
    assert failed["error_code"] == "GATEWAY_QUOTA_EXHAUSTED"
    reject_board = False
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    board = advance(client, settings, run_id)
    assert board["status"] == "WAITING_REVIEW", board
    assert board["pending_review"]["resource_type"] == "storyboard"
    assert review(client, board).status_code == 202
    assert advance(client, settings, run_id)["status"] == "SUCCEEDED"
    assert len(received) == 3 and received[1] == received[2]
