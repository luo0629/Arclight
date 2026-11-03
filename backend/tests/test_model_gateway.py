"""SVC-02 exercises public entry points, durable failures and shared accounting."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from arclight.application.model_gateway import admit, finish, invoke, route_id
from arclight.application.runtime import execute_run
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import (
    GatewayCapabilities,
    GatewayFailure,
    GatewayPolicy,
    GatewayRequest,
    GatewayRoute,
    GatewayUsage,
)
from arclight.domain.versions import digest
from arclight.persistence.base import utcnow
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt


def route(**patch):
    return GatewayRoute(
        **(
            dict(
                provider="fake",
                model="test-model",
                config_version="v1",
                credential_source="NONE",
                capabilities=GatewayCapabilities(
                    version="fake:1",
                    tasks=frozenset({"IMAGE", "script"}),
                    parameters=frozenset({"seed"}),
                ),
                maximum_cost=Decimal("2"),
                pricing_version="trusted:1",
            )
            | patch
        )
    )


def request(episode, selected, **patch):
    return GatewayRequest(
        **(
            dict(
                project_id=UUID(episode["project_id"]),
                episode_id=UUID(episode["id"]),
                actor="service-owner",
                operation_key=str(uuid4()),
                task="IMAGE",
                input_sha256=digest({"input": "private"}),
                parameters={"seed": 7},
                authorized_routes=frozenset({route_id(selected)}),
            )
            | patch
        )
    )


def test_existing_text_entry_uses_gateway(client, episode, settings):
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        json={"story": "a quiet forest"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    identity = UUID(response.json()["run_id"])
    execute_run(client.app.state.session_factory, settings, identity, 0)
    assert client.get(f"/api/v1/runs/{identity}").json()["status"] == "WAITING_REVIEW"
    with client.app.state.session_factory() as db:
        call = db.scalar(select(GatewayCall).where(GatewayCall.workflow_id == identity))
        assert call.task == "script" and call.maximum_cost == 0
        receipt = db.scalar(select(GatewayReceipt).where(GatewayReceipt.call_id == call.id))
        assert receipt.status == "SUCCEEDED" and receipt.actual_cost == 0


def test_gateway_success_and_idempotent_refusal(client, episode):
    from test_generation_budget import set_policy

    set_policy(client, episode["project_id"], "10")
    selected = route()
    call = request(episode, selected)
    factory = client.app.state.session_factory
    assert (
        invoke(
            factory,
            call,
            selected,
            lambda: "result",
            policy=GatewayPolicy(),
            usage=lambda _: GatewayUsage(images=1, actual_cost=Decimal("1")),
        )
        == "result"
    )
    with pytest.raises(DomainError, match="已有模型调用"):
        invoke(
            factory, call, selected, lambda: pytest.fail("duplicate billed"), policy=GatewayPolicy()
        )
    budget = client.get(f"/api/v1/projects/{episode['project_id']}/budget").json()
    assert Decimal(budget["actual_cost"]) == 1


def test_unknown_call_retains_budget_and_cannot_fallback(client, episode):
    from test_generation_budget import set_policy

    set_policy(client, episode["project_id"], "10")
    selected = route()
    call = request(episode, selected)
    called = []

    def broken():
        called.append(1)
        raise TimeoutError("secret-private-upstream-content")

    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            call,
            selected,
            broken,
            policy=GatewayPolicy(max_attempts=3),
            fallback=(selected, lambda: pytest.fail("unsafe fallback")),
        )
    assert caught.value.code == "GATEWAY_RECONCILIATION_REQUIRED" and called == [1]
    with client.app.state.session_factory() as db:
        row = db.scalar(select(GatewayCall).where(GatewayCall.operation_key == call.operation_key))
        receipt = db.scalar(select(GatewayReceipt).where(GatewayReceipt.call_id == row.id))
        assert receipt.status == "RESULT_UNKNOWN" and receipt.actual_cost is None
        assert "secret" not in str(receipt.usage_json)
    budget = client.get(f"/api/v1/projects/{episode['project_id']}/budget").json()
    assert Decimal(budget["pending_estimate"]) == 2


def api(episode):
    return f"/api/v1/projects/{episode['project_id']}/model-gateway"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"task": "AUDIO"}, "GATEWAY_CAPABILITY_REJECTED"),
        ({"parameters": {"unsupported": True}}, "GATEWAY_CAPABILITY_REJECTED"),
        ({"authorized_routes": frozenset()}, "GATEWAY_ROUTE_FORBIDDEN"),
        ({"episode_id": uuid4()}, "GATEWAY_SCOPE_INVALID"),
        ({"workflow_id": uuid4()}, "GATEWAY_SCOPE_INVALID"),
    ],
)
def test_rejection_happens_before_network_or_budget(client, episode, change, code):
    selected = route(maximum_cost=Decimal("0"))
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            request(episode, selected, **change),
            selected,
            lambda: pytest.fail("must reject before I/O"),
            policy=GatewayPolicy(),
        )
    assert caught.value.code == code
    assert client.get(api(episode) + "/calls").json() == []


@pytest.mark.parametrize(
    "limits,code",
    [
        ({"limit": "1"}, "BUDGET_EXCEEDED"),
        ({"episode_limit": "1"}, "BUDGET_EPISODE_EXCEEDED"),
        ({"task_limit": "1"}, "BUDGET_TASK_EXCEEDED"),
    ],
)
def test_text_budget_scopes_reject_before_network(client, episode, limits, code):
    from test_generation_budget import set_policy

    set_policy(client, episode["project_id"], **limits)
    selected = route()
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            request(episode, selected),
            selected,
            lambda: pytest.fail("over budget"),
            policy=GatewayPolicy(),
        )
    assert caught.value.code == code


@pytest.mark.parametrize(
    "fallback_kind,code",
    [
        ("compatible", None),
        ("unauthorized", "GATEWAY_ROUTE_FORBIDDEN"),
        ("incompatible", "GATEWAY_CAPABILITY_REJECTED"),
        ("expensive", "BUDGET_EXCEEDED"),
    ],
)
def test_fallback_requires_nonacceptance_capability_authorization_and_budget(
    client, episode, fallback_kind, code
):
    from test_generation_budget import set_policy

    set_policy(client, episode["project_id"], "3")
    primary = route()
    alternate = route(
        provider="alternate",
        maximum_cost=Decimal("4") if fallback_kind == "expensive" else Decimal("2"),
    )
    if fallback_kind == "incompatible":
        alternate = alternate.model_copy(
            update={
                "capabilities": GatewayCapabilities(version="audio:1", tasks=frozenset({"AUDIO"}))
            }
        )
    authorized = {route_id(primary)}
    if fallback_kind != "unauthorized":
        authorized.add(route_id(alternate))
    current = request(episode, primary, authorized_routes=frozenset(authorized))
    seen = []

    def rejected():
        seen.append("primary")
        raise GatewayFailure("PROVIDER_UNAVAILABLE", not_accepted=True, transient=True)

    def backup():
        seen.append("backup")
        return "ok"

    def run():
        return invoke(
            client.app.state.session_factory,
            current,
            primary,
            rejected,
            policy=GatewayPolicy(max_attempts=2),
            fallback=(alternate, backup),
            usage=lambda _: GatewayUsage(actual_cost=Decimal("1")),
        )

    if code:
        with pytest.raises(DomainError) as caught:
            run()
        assert caught.value.code == code
        assert seen == ["primary"]
    else:
        assert run() == "ok" and seen == ["primary", "backup"]
        rows = client.get(api(episode) + "/calls").json()
        assert [row["status"] for row in rows] == ["NOT_ACCEPTED", "SUCCEEDED"]
        assert rows[1]["route"]["provider"] == "alternate"


def test_retry_is_bounded_and_durable_across_invocations(client, episode):
    selected = route(maximum_cost=Decimal("0"))
    current = request(episode, selected)
    seen = []

    def reject():
        seen.append(1)
        raise GatewayFailure("PROVIDER_UNAVAILABLE", not_accepted=True, transient=True)

    with pytest.raises(DomainError):
        invoke(
            client.app.state.session_factory,
            current,
            selected,
            reject,
            policy=GatewayPolicy(max_attempts=2),
        )
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            current,
            selected,
            reject,
            policy=GatewayPolicy(max_attempts=2),
        )
    assert caught.value.code == "GATEWAY_ATTEMPT_LIMIT" and seen == [1, 1]


@pytest.mark.parametrize("scope", ["global", "user"])
def test_rate_limit_persists_across_sessions_and_expires(client, episode, monkeypatch, scope):
    from arclight.application import model_gateway

    selected = route(maximum_cost=Decimal("0"))
    policy = GatewayPolicy(**{f"{scope}_per_minute": 1})
    invoke(
        client.app.state.session_factory,
        request(episode, selected),
        selected,
        lambda: 1,
        policy=policy,
    )
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            request(episode, selected),
            selected,
            lambda: pytest.fail("rate limited"),
            policy=policy,
        )
    assert caught.value.code == "GATEWAY_RATE_LIMITED"
    later = utcnow() + timedelta(seconds=61)
    monkeypatch.setattr(model_gateway, "utcnow", lambda: later)
    assert (
        invoke(
            client.app.state.session_factory,
            request(episode, selected),
            selected,
            lambda: 2,
            policy=policy,
        )
        == 2
    )


def test_circuit_open_single_probe_recovery_pause_and_cas(client, episode, monkeypatch):
    from arclight.application import model_gateway

    selected = route(maximum_cost=Decimal("0"))
    policy = GatewayPolicy(failure_threshold=1, cooldown_seconds=10)

    def reject():
        raise GatewayFailure("PROVIDER_UNAVAILABLE", not_accepted=True)

    with pytest.raises(DomainError):
        invoke(
            client.app.state.session_factory,
            request(episode, selected),
            selected,
            reject,
            policy=policy,
        )
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            request(episode, selected),
            selected,
            lambda: pytest.fail("open circuit"),
            policy=policy,
        )
    assert caught.value.code == "GATEWAY_CIRCUIT_OPEN"
    later = utcnow() + timedelta(seconds=11)
    monkeypatch.setattr(model_gateway, "utcnow", lambda: later)
    with client.app.state.session_factory.begin() as db:
        probe = admit(db, request(episode, selected), selected, policy)
        probe_id = probe.id
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            request(episode, selected),
            selected,
            lambda: pytest.fail("only one recovery probe"),
            policy=policy,
        )
    assert caught.value.code == "GATEWAY_CIRCUIT_OPEN"
    with client.app.state.session_factory.begin() as db:
        finish(db, probe_id, "SUCCEEDED", GatewayUsage(actual_cost=Decimal("0")), policy)
    circuit = client.get(api(episode) + "/circuits").json()[0]
    assert circuit["failures"] == 0 and circuit["opened_until"] is None
    path = api(episode) + "/circuits/" + circuit["id"]
    body = {"expected_revision": circuit["revision"], "paused": True, "reason": "供应商维护"}
    assert client.patch(path, json=body).status_code == 200
    assert client.patch(path, json=body).status_code == 409
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            request(episode, selected),
            selected,
            lambda: pytest.fail("owner paused"),
            policy=policy,
        )
    assert caught.value.code == "GATEWAY_PAUSED"
    assert (
        client.patch(
            path, json=body | {"expected_revision": circuit["revision"] + 1, "paused": False}
        ).status_code
        == 200
    )
    assert (
        invoke(
            client.app.state.session_factory,
            request(episode, selected),
            selected,
            lambda: 1,
            policy=policy,
        )
        == 1
    )


def test_reconcile_is_evidence_based_append_only_and_idempotent(client, episode):
    from test_generation_budget import set_policy

    set_policy(client, episode["project_id"], "2")
    selected = route()
    current = request(episode, selected)
    with pytest.raises(DomainError):
        invoke(
            client.app.state.session_factory,
            current,
            selected,
            lambda: (_ for _ in ()).throw(TimeoutError()),
            policy=GatewayPolicy(),
        )
    row = client.get(api(episode) + "/calls").json()[0]
    path = api(episode) + f"/calls/{row['id']}/reconcile"
    assert (
        client.post(
            path,
            json={
                "status": "NOT_ACCEPTED",
                "usage": {"actual_cost": "1"},
                "evidence": "reject receipt",
            },
        ).status_code
        == 422
    )
    body = {
        "status": "NOT_ACCEPTED",
        "usage": {"actual_cost": "0"},
        "evidence": "provider-ticket:001",
    }
    settled = client.post(path, json=body)
    assert settled.status_code == 200, settled.text
    assert client.post(path, json=body).json() == settled.json()
    assert client.post(path, json=body | {"evidence": "another-ticket"}).status_code == 409
    assert (
        invoke(
            client.app.state.session_factory,
            current,
            selected,
            lambda: "retry",
            policy=GatewayPolicy(max_attempts=2),
            usage=lambda _: GatewayUsage(actual_cost=Decimal("3")),
        )
        == "retry"
    )
    budget = client.get(f"/api/v1/projects/{episode['project_id']}/budget").json()
    assert Decimal(budget["actual_cost"]) == 3 and Decimal(budget["pending_estimate"]) == 0
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GatewayReceipt)) == 3


def test_fake_media_enters_same_gateway_without_double_reservation(client, episode, monkeypatch):
    from test_generation_budget import priced, set_policy
    from test_generation_delivery import Provider, deliver
    from test_generation_jobs import capture, capture_ok

    from arclight.domain.generation_requests import GenerationResult
    from arclight.providers.media_submission import SubmissionReceipt

    set_policy(client, episode["project_id"], "3")
    priced(monkeypatch, "2")
    selected = route(maximum_cost=Decimal("1"))
    invoke(
        client.app.state.session_factory,
        request(episode, selected),
        selected,
        lambda: 1,
        policy=GatewayPolicy(),
        usage=lambda _: GatewayUsage(actual_cost=Decimal("1")),
    )
    job = capture_ok(client, episode["project_id"])
    provider = Provider(
        receipt=SubmissionReceipt(
            status="SUCCEEDED",
            cost=Decimal("2"),
            result=GenerationResult(output_handles=["fake/image/1"]),
        )
    )
    assert deliver(client, job, provider) == "SUCCEEDED" and len(provider.requests) == 1
    assert capture(client, episode["project_id"]).json()["error"]["code"] == "BUDGET_EXCEEDED"
    rows = client.get(api(episode) + "/calls").json()
    assert len(rows) == 2 and rows[1]["generation_attempt_id"]
    assert rows[1]["route"]["capabilities"]["media"]["execution_mode"] == "BOTH"
    budget = client.get(f"/api/v1/projects/{episode['project_id']}/budget").json()
    assert Decimal(budget["actual_cost"]) == 3 and Decimal(budget["pending_estimate"]) == 0


@pytest.mark.parametrize("action", ["update", "delete", "replace", "truncate"])
def test_database_enforces_immutable_calls_and_receipts(client, episode, action):
    selected = route(maximum_cost=Decimal("0"))
    invoke(
        client.app.state.session_factory,
        request(episode, selected),
        selected,
        lambda: 1,
        policy=GatewayPolicy(),
        usage=lambda _: GatewayUsage(actual_cost=Decimal("0")),
    )
    factory = client.app.state.session_factory
    dialect = factory.kw["bind"].dialect.name
    if (
        action == "replace"
        and dialect != "sqlite"
        or action == "truncate"
        and dialect != "postgresql"
    ):
        pytest.skip("database-specific immutable operation")
    for table in ("model_gateway_calls", "model_gateway_receipts"):
        statement = {
            "update": f"UPDATE {table} SET actor='rewritten'",
            "delete": f"DELETE FROM {table}",
            "replace": f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
            "truncate": f"TRUNCATE {table} CASCADE",
        }[action]
        with factory.begin() as db, pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"):
            db.execute(text(statement))


def test_downgrade_refuses_to_discard_usage_history(client, episode, migrated):
    from alembic import command

    selected = route(maximum_cost=Decimal("0"))
    invoke(
        client.app.state.session_factory,
        request(episode, selected),
        selected,
        lambda: 1,
        policy=GatewayPolicy(),
    )
    with pytest.raises(RuntimeError, match="history exists"):
        command.downgrade(migrated, "0039")
    assert len(client.get(api(episode) + "/calls").json()) == 1


def test_real_text_transport_uses_frozen_connection_and_records_sanitized_usage(
    client, settings, monkeypatch, caplog
):
    import json

    import httpx
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    from test_generation_budget import set_policy
    from test_model_services import account, profile

    from arclight.providers.text import MockTextProvider

    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=Decimal("2"), pricing_version="reviewed:1"
    )
    account(client)
    secret = "gateway-frozen-test-key"
    assert (
        client.put(
            "/api/v1/me/model-services/text", json=profile(mode="custom", api_key=secret)
        ).status_code
        == 200
    )
    project = client.post("/api/v1/projects", json={"name": "gateway snapshot"}).json()
    episode = client.post(
        f"/api/v1/projects/{project['id']}/episodes", json={"episode_no": 1, "title": "test"}
    ).json()
    set_policy(client, project["id"], "10")
    started = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        json={"story": "quiet forest"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    identity = UUID(started.json()["run_id"])
    assert (
        client.put(
            "/api/v1/me/model-services/text",
            json=profile(
                expected_revision=1,
                mode="custom",
                api_key="changed-test-key",
                model="changed-model",
            ),
        ).status_code
        == 200
    )
    settings.model_gateway_policy = GatewayPolicy()
    captured = []

    def handler(req):
        body = json.loads(req.content)
        captured.append((str(req.url), body["model"]))
        assert req.headers["authorization"] == "Bearer " + secret
        content = MockTextProvider().script("quiet forest", {}, 30000)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content.model_dump_json()}}],
                "usage": {"prompt_tokens": 17, "completion_tokens": 23},
            },
        )

    monkeypatch.setattr(
        "arclight.providers.text.PublicHTTPTransport",
        lambda *args, **kwargs: httpx.MockTransport(handler),
    )
    execute_run(client.app.state.session_factory, settings, identity, 0)
    assert client.get(f"/api/v1/runs/{identity}").json()["status"] == "WAITING_REVIEW"
    assert captured == [("https://api.openai.com/v1/chat/completions", "text-model")]
    rows = client.get(api(episode) + "/calls").json()
    assert len(rows) == 1 and rows[0]["route"]["credential_source"] == "ACCOUNT_SNAPSHOT"
    assert (
        rows[0]["route"]["pricing_version"] == "reviewed:1"
        and Decimal(rows[0]["maximum_cost"]) == 2
    )
    assert rows[0]["usage"]["input_tokens"] == 17 and rows[0]["usage"]["output_tokens"] == 23
    assert rows[0]["usage"]["actual_cost"] is None and rows[0]["cost_is_estimate"]
    public = str(rows) + client.get(f"/api/v1/runs/{identity}").text + caplog.text
    assert secret not in public and "changed-test-key" not in public
    assert "encrypted_key" not in str(rows) and "base_url" not in str(rows)


@pytest.mark.parametrize("role", ["viewer", "removed", "disabled", "outsider"])
def test_execution_rechecks_actor_scope_and_read_isolation(client, episode, role):
    from test_collaboration import register

    from arclight.persistence.models import ProjectMember, User

    member = register(client, f"{role}@example.com")
    identity = UUID(member["user"]["id"])
    with client.app.state.session_factory.begin() as db:
        if role in {"viewer", "disabled"}:
            db.add(
                ProjectMember(
                    project_id=UUID(episode["project_id"]),
                    user_id=identity,
                    role="viewer" if role == "viewer" else "editor",
                )
            )
        if role == "disabled":
            db.get(User, identity).disabled = True
    selected = route(maximum_cost=Decimal("0"))
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            request(episode, selected, actor=str(identity)),
            selected,
            lambda: pytest.fail("actor lost permission"),
            policy=GatewayPolicy(),
        )
    assert caught.value.code in {"FORBIDDEN", "NOT_FOUND"}
    assert client.get(api(episode) + "/calls").status_code == (
        200 if role == "viewer" else 401 if role == "disabled" else 404
    )


def test_viewer_cannot_pause_or_reconcile_and_cross_project_is_hidden(client, episode):
    from test_collaboration import create_project, register

    from arclight.persistence.models import ProjectMember

    selected = route(maximum_cost=Decimal("0"))
    invoke(
        client.app.state.session_factory,
        request(episode, selected),
        selected,
        lambda: 1,
        policy=GatewayPolicy(),
    )
    row = client.get(api(episode) + "/calls").json()[0]
    viewer = register(client, "viewer-diagnostics@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(episode["project_id"]),
                user_id=UUID(viewer["user"]["id"]),
                role="viewer",
            )
        )
    assert (
        client.patch(
            api(episode) + "/circuits/" + row["circuit_id"],
            json={"expected_revision": 1, "paused": True, "reason": "maintenance"},
        ).status_code
        == 403
    )
    body = {"status": "SUCCEEDED", "usage": {"actual_cost": "0"}, "evidence": "test receipt"}
    assert client.post(api(episode) + f"/calls/{row['id']}/reconcile", json=body).status_code == 403
    other = create_project(client)
    assert (
        client.post(
            f"/api/v1/projects/{other['id']}/model-gateway/calls/{row['id']}/reconcile", json=body
        ).status_code
        == 404
    )


@pytest.mark.parametrize("case", ["budget", "duplicate", "rate"])
def test_postgres_concurrent_admission_is_serialized(client, episode, settings, case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from test_concurrency import require_postgres
    from test_generation_budget import set_policy

    require_postgres(settings)
    set_policy(client, episode["project_id"], "2")
    selected = route()
    first = request(episode, selected)
    second = first if case == "duplicate" else request(episode, selected)
    policy = GatewayPolicy(global_per_minute=1) if case == "rate" else GatewayPolicy()
    if case != "budget":
        set_policy(client, episode["project_id"], "10")
    barrier = Barrier(2)

    def run(current):
        barrier.wait(timeout=5)
        try:
            with client.app.state.session_factory.begin() as db:
                admit(db, current, selected, policy)
            return "admitted"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(run, (first, second)))
    assert sorted(outcomes) == sorted(
        [
            "admitted",
            {
                "budget": "BUDGET_EXCEEDED",
                "duplicate": "GATEWAY_RECONCILIATION_REQUIRED",
                "rate": "GATEWAY_RATE_LIMITED",
            }[case],
        ]
    )


def test_postgres_text_and_media_compete_for_the_same_budget(
    client, episode, settings, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from test_concurrency import require_postgres
    from test_generation_budget import priced, set_policy
    from test_generation_jobs import payload

    from arclight.application.generation_jobs import create_job
    from arclight.domain.generation_requests import GenerationCreate

    require_postgres(settings)
    set_policy(client, episode["project_id"], "2")
    priced(monkeypatch, "2")
    selected = route()
    barrier = Barrier(2)

    def run(kind):
        barrier.wait(timeout=10)
        try:
            with client.app.state.session_factory.begin() as db:
                if kind == "text":
                    admit(db, request(episode, selected, task="script"), selected, GatewayPolicy())
                else:
                    create_job(
                        db,
                        UUID(episode["project_id"]),
                        "service-owner",
                        str(uuid4()),
                        GenerationCreate.model_validate(payload()),
                    )
            return "admitted"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(run, ("text", "media")))
    assert sorted(outcomes) == ["BUDGET_EXCEEDED", "admitted"]


def test_real_deadline_preserves_uncertainty_and_disallows_fallback(client, episode):
    from threading import Event

    selected = route(maximum_cost=Decimal("0"))
    release = Event()
    entered = Event()

    def slow():
        entered.set()
        release.wait(timeout=5)
        return "late"

    try:
        with pytest.raises(DomainError) as caught:
            invoke(
                client.app.state.session_factory,
                request(episode, selected),
                selected,
                slow,
                # Admission also consumes the deadline; leave room for real DB I/O
                # so this test reaches the deliberately blocked provider on slow CI.
                policy=GatewayPolicy(timeout_seconds=1, max_attempts=2),
                fallback=(selected, lambda: pytest.fail("unknown cannot fallback")),
            )
        assert entered.is_set() and caught.value.code == "GATEWAY_RECONCILIATION_REQUIRED"
        rows = client.get(api(episode) + "/calls").json()
        assert len(rows) == 1 and rows[0]["status"] == "RESULT_UNKNOWN"
    finally:
        release.set()
