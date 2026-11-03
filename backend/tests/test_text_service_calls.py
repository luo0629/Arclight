"""Personal text calls need no quotation and expose safe provider rejections."""

from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from test_model_gateway import api
from test_model_services import account, profile

from arclight.application.runtime import execute_run
from arclight.domain.model_gateway import GatewayPolicy
from arclight.providers.text import MockTextProvider
from arclight.workflows.checkpoints import open_checkpointer

SECRET = "private-upstream-api-key"
UPSTREAM_DETAIL = "private-provider-response-data"


@pytest.fixture
def personal_text(client, settings):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    settings.model_gateway_policy = GatewayPolicy(max_attempts=3)
    account(client)
    response = client.put(
        "/api/v1/me/model-services/text",
        json=profile(
            mode="custom", base_url="https://personal-upstream.example/v1", api_key=SECRET
        ),
    )
    assert response.status_code == 200, response.text
    project = client.post("/api/v1/projects", json={"name": "personal billing"}).json()
    return client.post(
        f"/api/v1/projects/{project['id']}/episodes", json={"episode_no": 1, "title": "text"}
    ).json()


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (402, {}, "SERVICE_BALANCE_INSUFFICIENT"),
        (429, {"error": {"code": "insufficient_quota"}}, "SERVICE_BALANCE_INSUFFICIENT"),
        (429, {"error": {"type": "insufficient_quota"}}, "SERVICE_BALANCE_INSUFFICIENT"),
        (
            429,
            {"error": {"type": "insufficient_quota", "code": "credit_balance_exhausted"}},
            "SERVICE_BALANCE_INSUFFICIENT",
        ),
        (
            429,
            {"error": {"type": "insufficient_quota", "code": "project_spend_limit_exceeded"}},
            "SERVICE_QUOTA_EXCEEDED",
        ),
        (403, {"code": "Arrearage"}, "SERVICE_BALANCE_INSUFFICIENT"),
        (403, {"error": {"message": "余额不足"}}, "SERVICE_BALANCE_INSUFFICIENT"),
        (429, {"error": {"code": "rate_limit_exceeded"}}, "SERVICE_RATE_LIMITED"),
        (401, {"error": {"code": "invalid_api_key"}}, "SERVICE_AUTHENTICATION_FAILED"),
        (500, {"error": {"code": "insufficient_quota"}}, "GATEWAY_RECONCILIATION_REQUIRED"),
        (400, ["unexpected", "error", "shape"], "GATEWAY_RECONCILIATION_REQUIRED"),
    ],
)
def test_provider_rejection_reaches_task_without_auto_retry_or_sensitive_details(
    client, settings, personal_text, monkeypatch, caplog, status, body, expected
):
    calls = []

    def handler(request):
        calls.append(request)
        # Malicious or verbose upstream fields must never reach public history,
        # logs, or LangGraph's persisted exception strings.
        response = (
            body | {"debug": f"{SECRET} {UPSTREAM_DETAIL}"} if isinstance(body, dict) else body
        )
        return httpx.Response(status, json=response)

    monkeypatch.setattr(
        "arclight.providers.text.PublicHTTPTransport", lambda *_: httpx.MockTransport(handler)
    )
    accepted = client.post(
        f"/api/v1/episodes/{personal_text['id']}/script-runs",
        json={"story": "quiet forest"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    # Duplicate delivery must not call the provider again after the failure.
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "FAILED" and run["error_code"] == expected
    assert len(calls) == 1
    rows = client.get(api(personal_text) + "/calls").json()
    assert len(rows) == 1 and rows[0]["maximum_cost"] is None
    rejected = expected.startswith("SERVICE_")
    assert rows[0]["status"] == ("NOT_ACCEPTED" if rejected else "RESULT_UNKNOWN")
    assert rows[0]["usage"]["actual_cost"] == ("0" if rejected else None)
    if rejected:
        assert rows[0]["error_code"] == expected
    events = client.get(f"/api/v1/runs/{run_id}/events").text
    with open_checkpointer(settings) as saver:
        checkpoints = list(saver.list({"configurable": {"thread_id": run_id}}))
    public = str(rows) + str(run) + events + str(checkpoints) + caplog.text
    assert SECRET not in public and UPSTREAM_DETAIL not in public


@pytest.mark.parametrize("endpoint", ["story-analysis-runs", "story-outline-runs"])
def test_balance_failure_survives_all_text_workflow_boundaries(
    client, settings, personal_text, monkeypatch, endpoint
):
    monkeypatch.setattr(
        "arclight.providers.text.PublicHTTPTransport",
        lambda *_: httpx.MockTransport(lambda _: httpx.Response(402)),
    )
    started = client.post(
        f"/api/v1/episodes/{personal_text['id']}/{endpoint}",
        json={"story": "quiet forest"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "FAILED"
    assert run["error_code"] == "SERVICE_BALANCE_INSUFFICIENT"


def test_user_can_retry_after_recharging_without_reconciliation(
    client, settings, personal_text, monkeypatch
):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(402)
        content = MockTextProvider().script("quiet forest", {}, 30000)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content.model_dump_json()}}]}
        )

    monkeypatch.setattr(
        "arclight.providers.text.PublicHTTPTransport", lambda *_: httpx.MockTransport(handler)
    )
    started = client.post(
        f"/api/v1/episodes/{personal_text['id']}/script-runs",
        json={"story": "quiet forest"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    failed = client.get(f"/api/v1/runs/{run_id}").json()
    assert failed["error_code"] == "SERVICE_BALANCE_INSUFFICIENT"
    retried = client.post(f"/api/v1/runs/{run_id}/retry")
    assert retried.status_code == 202, retried.text
    execute_run(
        client.app.state.session_factory, settings, UUID(run_id), retried.json()["revision"]
    )
    assert len(calls) == 2
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "WAITING_REVIEW"
    assert len(client.get(api(personal_text) + "/calls").json()) == 2
