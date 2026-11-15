import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import select

from arclight.api import event_streams as transport
from arclight.application.event_streams import PAGE_SIZE, Cursor, read_events
from arclight.core.errors import DomainError
from arclight.core.security import decode_token
from arclight.main import create_app
from arclight.persistence.models import (
    AuditEvent,
    AuthSession,
    Project,
    ProjectMember,
    WorkflowEvent,
    WorkflowRun,
)


@pytest.fixture
def feed(client, episode):
    result = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        headers={"Idempotency-Key": "event-stream"},
        json={"story": "雨夜，一封信改变了旅人的计划。"},
    )
    assert result.status_code == 202, result.text
    return {
        "project": UUID(episode["project_id"]),
        "episode": UUID(episode["id"]),
        "run": UUID(result.json()["run_id"]),
        "factory": client.app.state.session_factory,
    }


def page(feed, cursor=None, *, tail=False, run=False):
    with feed["factory"].begin() as db:
        return read_events(
            db,
            "service-owner",
            project_id=feed["project"],
            run_id=feed["run"] if run else None,
            cursor=Cursor.parse(cursor),
            tail=tail,
        )


def append(feed, *, when=None, identity=None, name="workflow.finished", secret="private"):
    with feed["factory"].begin() as db:
        event = WorkflowEvent(
            id=identity or uuid4(),
            run_id=feed["run"],
            event_type=name,
            payload_json={"system_prompt": secret, "token": secret, "stack": secret},
            **({"created_at": when} if when else {}),
        )
        db.add(event)
        db.flush()
        return event.id


def frames(response):
    result = []
    text = response if isinstance(response, str) else response.text
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        item = {}
        for line in frame.splitlines():
            if ":" in line and not line.startswith(":"):
                key, value = line.split(":", 1)
                item[key] = value.removeprefix(" ")
        if item:
            if "data" in item:
                item["data"] = json.loads(item["data"])
            result.append(item)
    return result


def register(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"events-{uuid4()}@example.com",
            "password": "events-test-password",
            "display_name": "事件订阅者",
        },
    )
    assert response.status_code == 201, response.text
    result = response.json()
    return result["access_token"], UUID(result["user"]["id"])


@pytest.mark.parametrize(
    "value",
    [
        "bad",
        "v1:w:not-a-uuid:1",
        "v1:w:00000000-0000-0000-0000-000000000001:0",
        "v1:w:00000000-0000-0000-0000-000000000001:-1",
        "v1:w:00000000-0000-0000-0000-000000000001:1\ninjected",
    ],
)
def test_cursor_rejects_malformed_values(value):
    with pytest.raises(DomainError) as error:
        Cursor.parse(value)
    assert error.value.code == "EVENT_CURSOR_INVALID"


def test_stream_headers_native_encoding_tail_and_old_json_contract(client, feed, monkeypatch):
    monkeypatch.setattr(transport, "CONNECTION_SECONDS", 0)
    path = f"/api/v1/runs/{feed['run']}/events"
    legacy = client.get(path + "?limit=1&offset=0")
    assert legacy.status_code == 200 and len(legacy.json()) == 1
    response = client.get(path + "/stream")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["x-accel-buffering"] == "no"
    events = frames(response)
    assert [row["event"] for row in events] == ["stream.ready", "stream.reconnect"]
    assert events[0]["data"] == {"project_id": str(feed["project"])}
    assert events[0]["id"] == page(feed, tail=True, run=True).cursor
    assert events[0]["retry"] == "1000"
    # The existing JSON list shape, pagination, identity and payload are unchanged.
    assert client.get(path + "?limit=1&offset=0").json() == legacy.json()
    assert client.get(path + "?limit=1&offset=1").json() == []


def test_persisted_pages_resume_across_application_instances(client, feed, settings, monkeypatch):
    monkeypatch.setattr(transport, "CONNECTION_SECONDS", 0)
    cursor = page(feed, tail=True).cursor
    now = datetime.now(UTC) + timedelta(seconds=1)
    identities = [append(feed, when=now + timedelta(microseconds=i)) for i in range(205)]
    delivered = []
    for _ in range(3):
        # A new app has no in-memory replay queue or connection state to reuse.
        with TestClient(create_app(settings)) as restarted:
            response = restarted.get(
                f"/api/v1/projects/{feed['project']}/events/stream",
                headers={"Authorization": "Bearer test-token", "Last-Event-ID": cursor},
            )
        assert response.status_code == 200, response.text
        events = frames(response)
        assert events[0]["id"] == cursor  # never acknowledge unsent page records
        data = [row for row in events if row["event"] == "run.completed"]
        assert len(data) <= PAGE_SIZE
        delivered.extend(UUID(row["data"]["source_id"]) for row in data)
        cursor = data[-1]["id"]
    assert delivered == identities
    assert len(set(delivered)) == 205
    assert page(feed, cursor).events == ()


def test_late_commit_before_anchor_resets_and_replays_with_stable_source_identity(feed):
    before = page(feed, tail=True).cursor
    now = datetime.now(UTC) + timedelta(seconds=1)
    high = append(feed, when=now, identity=UUID(int=500))
    original = page(feed, before)
    assert [row.data["source_id"] for row in original.events] == [str(high)]
    # Same-time/lower-ID event becomes visible after the higher-ID cursor.
    low = append(feed, when=now, identity=UUID(int=400))
    replay = page(feed, original.cursor)
    assert replay.reset
    ids = [row.data["source_id"] for row in replay.events]
    assert ids.index(str(low)) < ids.index(str(high))
    again = next(row for row in replay.events if row.data["source_id"] == str(high))
    assert again.data == original.events[0].data
    assert page(feed, replay.cursor).events == ()


def test_only_committed_records_are_visible_and_rollback_has_no_notification(feed):
    cursor = page(feed, tail=True).cursor
    with feed["factory"]() as writer:
        row = WorkflowEvent(run_id=feed["run"], event_type="workflow.failed", payload_json={})
        writer.add(row)
        writer.flush()
        assert page(feed, cursor).events == ()
        writer.rollback()
    assert page(feed, cursor).events == ()
    identity = append(feed)
    assert [row.data["source_id"] for row in page(feed, cursor).events] == [str(identity)]


@pytest.mark.parametrize("kind", ["unknown", "wrong-position", "other-project", "other-run"])
def test_invalid_scope_or_position_requires_explicit_reset_without_exposing_payload(
    client,
    feed,
    kind,
    monkeypatch,
):
    monkeypatch.setattr(transport, "CONNECTION_SECONDS", 0)
    baseline = Cursor.parse(page(feed, tail=True, run=True).cursor)
    assert baseline is not None
    if kind == "wrong-position":
        cursor = Cursor(baseline.source, baseline.identity, baseline.position + 1)
    else:
        cursor = Cursor("w", uuid4(), 1)
        if kind in {"other-project", "other-run"}:
            with feed["factory"].begin() as db:
                if kind == "other-project":
                    project = Project(owner_id="service-owner", name="另一个项目")
                    db.add(project)
                    db.flush()
                    from arclight.persistence.models import Episode

                    episode = Episode(
                        project_id=project.id,
                        episode_no=1,
                        title="私密集",
                        target_duration_ms=30000,
                    )
                    db.add(episode)
                    db.flush()
                    episode_id = episode.id
                else:
                    episode_id = feed["episode"]
                run = WorkflowRun(
                    episode_id=episode_id,
                    trace_id="private-trace",
                    idempotency_key=uuid4().hex,
                    request_fingerprint="x" * 64,
                    input_json={},
                )
                db.add(run)
                db.flush()
                db.add(
                    WorkflowEvent(
                        id=cursor.identity,
                        run_id=run.id,
                        event_type="private-secret",
                        payload_json={"token": "secret"},
                    )
                )
    response = client.get(
        f"/api/v1/runs/{feed['run']}/events/stream",
        headers={"Last-Event-ID": cursor.encode()},
    )
    assert response.status_code == 200
    result = frames(response)
    assert result[0]["id"] == ""
    assert result[1]["event"] == "stream.reset" and result[1]["id"] == ""
    assert "private-secret" not in response.text and "private-trace" not in response.text
    assert "secret" not in response.text


def test_notifications_are_metadata_only_and_untrusted_names_are_not_public(client, feed):
    cursor = page(feed, tail=True).cursor
    identity = append(feed, name="internal.system_prompt", secret="SENSITIVE-CONTENT")
    with feed["factory"].begin() as db:
        db.add(
            AuditEvent(
                project_id=feed["project"],
                actor_id="private-actor",
                action="private.stack",
                resource_id=uuid4(),
                details_json={"credential": "SENSITIVE-CONTENT"},
            )
        )
    events = page(feed, cursor).events
    assert {e.event for e in events} == {"run.status_changed", "project.changed"}
    assert any(e.data["source_id"] == str(identity) for e in events)
    for event in events:
        assert set(event.data) == {"source", "source_id", "project_id", "created_at"}
    public = json.dumps([e.data for e in events])
    for private in ("SENSITIVE-CONTENT", "private-actor", "system_prompt", "credential", "stack"):
        assert private not in public


@pytest.mark.parametrize("scope", ["projects", "runs"])
def test_initial_auth_cursor_and_scope_failures_use_existing_http_errors(
    client,
    feed,
    scope,
    monkeypatch,
):
    monkeypatch.setattr(transport, "CONNECTION_SECONDS", 0)
    resource = feed["project" if scope == "projects" else "run"]
    path = f"/api/v1/{scope}/{resource}/events/stream"
    assert client.get(path, headers={"Authorization": ""}).status_code == 401
    invalid = client.get(path, headers={"Last-Event-ID": "invalid"})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "EVENT_CURSOR_INVALID"
    assert invalid.json()["error"]["request_id"]
    assert client.get(path, headers={"Last-Event-ID": "x" * 161}).status_code == 422
    assert client.get(f"/api/v1/{scope}/{uuid4()}/events/stream").status_code == 404
    token, _ = register(client)
    assert client.get(path, headers={"Authorization": f"Bearer {token}"}).status_code == 404


def test_viewer_can_subscribe_to_archived_project_but_current_revocation_is_rechecked(
    client,
    feed,
    monkeypatch,
):
    monkeypatch.setattr(transport, "CONNECTION_SECONDS", 0)
    token, user_id = register(client)
    with feed["factory"].begin() as db:
        member = ProjectMember(project_id=feed["project"], user_id=user_id, role="viewer")
        db.add(member)
        db.get(Project, feed["project"]).archived = True
    path = f"/api/v1/projects/{feed['project']}/events/stream"
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(path, headers=headers).status_code == 200
    with feed["factory"].begin() as db:
        member = db.scalar(select(ProjectMember).where(ProjectMember.user_id == user_id))
        db.delete(member)
    assert client.get(path, headers=headers).status_code == 404


async def observe(app, path, token, receive_frame):
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    messages = []

    async def send(message):
        messages.append(message)
        if (
            message["type"] == "http.response.body"
            and message.get("body")
            and receive_frame(message["body"])
        ):
            incoming.put_nowait({"type": "http.disconnect"})

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "http_version": "1.1",
        "headers": [(b"host", b"testserver"), (b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    await asyncio.wait_for(app(scope, incoming.get, send), timeout=10)
    return b"".join(m.get("body", b"") for m in messages).decode()


def test_live_asgi_notification_arrives_without_closing_response_or_holding_a_session(
    client,
    feed,
    monkeypatch,
):
    monkeypatch.setattr(transport, "POLL_SECONDS", 0)
    identity = None

    def receive(body):
        nonlocal identity
        assert client.app.state.engine.pool.checkedout() == 0
        if b"event: stream.ready" in body:
            identity = append(feed)
        return b"event: run.completed" in body

    text = asyncio.run(
        observe(
            client.app, f"/api/v1/projects/{feed['project']}/events/stream", "test-token", receive
        )
    )
    assert any(row.get("data", {}).get("source_id") == str(identity) for row in frames(text))
    assert "stream.reconnect" not in text
    assert client.app.state.engine.pool.checkedout() == 0


@pytest.mark.parametrize("change", ["logout", "disabled", "membership", "deleted"])
def test_open_stream_stops_after_current_session_or_project_access_is_revoked(
    client,
    feed,
    monkeypatch,
    settings,
    change,
):
    monkeypatch.setattr(transport, "POLL_SECONDS", 0)
    token, user_id = register(client)
    claims = decode_token(token, settings, "access")
    with feed["factory"].begin() as db:
        db.add(ProjectMember(project_id=feed["project"], user_id=user_id, role="viewer"))

    def receive(body):
        if b"event: stream.ready" in body:
            with feed["factory"].begin() as db:
                if change == "logout":
                    db.get(AuthSession, UUID(claims["sid"])).revoked = True
                elif change == "disabled":
                    from arclight.persistence.models import User

                    db.get(User, user_id).disabled = True
                elif change == "membership":
                    db.delete(
                        db.scalar(select(ProjectMember).where(ProjectMember.user_id == user_id))
                    )
                else:
                    db.get(Project, feed["project"]).deleted_at = datetime.now(UTC)
            append(feed, secret="AFTER-REVOCATION")
        return b"event: stream.error" in body

    text = asyncio.run(
        observe(client.app, f"/api/v1/projects/{feed['project']}/events/stream", token, receive)
    )
    assert "stream.error" in text
    expected = 401 if change in {"logout", "disabled"} else 404
    errors = [row["data"] for row in frames(text) if row.get("event") == "stream.error"]
    assert len(errors) == 1 and errors[0]["status"] == expected
    assert "AFTER-REVOCATION" not in text and "event: run.completed" not in text
    assert client.app.state.engine.pool.checkedout() == 0


def test_heartbeat_and_disconnect_use_controlled_time_without_extra_events(
    client, feed, monkeypatch
):
    now = [0.0]
    monkeypatch.setattr(transport, "monotonic", lambda: now[0])

    def receive(body):
        if b"event: stream.ready" in body:
            now[0] = transport.HEARTBEAT_SECONDS + 1
        return b": keep-alive" in body

    text = asyncio.run(
        observe(
            client.app, f"/api/v1/projects/{feed['project']}/events/stream", "test-token", receive
        )
    )
    assert ": keep-alive" in text
    assert "run.completed" not in text and "stream.reconnect" not in text
    assert client.app.state.engine.pool.checkedout() == 0


def test_credentials_revalidated_each_page_even_after_successful_initial_load(client, feed):
    from starlette.requests import Request

    request = Request({"type": "http", "app": client.app, "headers": []})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-token")
    initial = transport.load_page(request, credentials, feed["project"], None, None, True)
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"environment": "production"}
    )
    with pytest.raises(DomainError) as error:
        transport.load_page(request, credentials, feed["project"], None, initial.cursor)
    assert error.value.status_code == 401
