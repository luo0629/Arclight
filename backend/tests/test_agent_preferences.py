"""Human confirmation, immutable history and real project-gate concurrency."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta, timezone
from io import StringIO
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from test_collaboration import create_project, register
from test_concurrency import require_postgres

from arclight.application import context_builder
from arclight.application.agent_preferences import (
    confirm_preference,
    preference_snapshot,
    preference_source,
    validate_preference_source,
)
from arclight.application.commands import start_run
from arclight.core.errors import DomainError
from arclight.domain.agent_memory import PreferenceChange
from arclight.persistence.agent_preferences import AgentPreference
from arclight.persistence.models import AuditEvent, ProjectMember, User


def preference_url(project_id):
    return f"/api/v1/projects/{project_id}/agent-preferences"


def change_body(**patch):
    return {
        "creation_key": str(uuid4()),
        "expected_revision": 0,
        "preference_name": "narrative_tone",
        "value": "克制、温暖",
        "confirmation": "CONFIRM",
        **patch,
    }


def confirm(client, project_id, **patch):
    response = client.post(preference_url(project_id), json=change_body(**patch))
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "patch",
    [
        {"confirmation": None},
        {"confirmation": "MODEL_APPROVED"},
        {"confirmation": "RULE"},
        {"confirmed_by": "service-owner"},
        {"rule_id": "auto-promote-model-output"},
        {"preference_name": "budget_limit"},
        {"preference_name": "locked_visual_fields"},
        {"value": "x" * 1001},
        {"value": "   "},
        {"value": {"domain_facts": ["invented fact"]}},
    ],
)
def test_unconfirmed_or_unbounded_preference_writes_are_rejected(client, episode, patch):
    response = client.post(preference_url(episode["project_id"]), json=change_body(**patch))
    assert response.status_code == 422, response.text
    missing = change_body()
    missing.pop("confirmation")
    assert client.post(preference_url(episode["project_id"]), json=missing).status_code == 422
    with client.app.state.session_factory() as db:
        assert db.scalar(select(AgentPreference)) is None
        assert (
            db.scalar(select(AuditEvent).where(AuditEvent.action == "agent.preference_confirmed"))
            is None
        )


def test_preference_versions_replay_cas_retire_and_preserve_exact_history(client, episode):
    project_id = episode["project_id"]
    body = change_body()
    first = client.post(preference_url(project_id), json=body)
    assert first.status_code == 201, first.text
    first = first.json()
    replay = client.post(preference_url(project_id), json=body)
    assert replay.status_code == 200 and replay.json() == first
    assert (
        client.post(preference_url(project_id), json=body | {"value": "不同偏好"}).status_code
        == 409
    )
    stale = client.post(preference_url(project_id), json=change_body(value="第二版"))
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "AGENT_PREFERENCE_CONFLICT"
    second = confirm(client, project_id, expected_revision=1, value="第二版")
    retired = confirm(client, project_id, expected_revision=2, value=None)
    assert [first["revision"], second["revision"], retired["revision"]] == [1, 2, 3]
    assert client.get(preference_url(project_id)).json() == {"preferences": [retired]}
    assert client.post(preference_url(project_id), json=body).json() == first
    with client.app.state.session_factory() as db:
        historic = db.get(AgentPreference, UUID(first["id"]))
        assert preference_snapshot(historic) == first
        validate_preference_source(db, UUID(project_id), preference_source(first))
        events = list(
            db.scalars(select(AuditEvent).where(AuditEvent.action == "agent.preference_confirmed"))
        )
        assert len(events) == 3
        assert all(row.actor_id == "service-owner" for row in events)
        db.expunge(historic)
        stamp = historic.created_at
        historic.created_at = (
            stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
        ).astimezone(timezone(timedelta(hours=9)))
        assert first["confirmed_at"].endswith("+00:00")
        assert preference_snapshot(historic) == first
        assert preference_source(preference_snapshot(historic)) == preference_source(first)


def test_preference_confirmation_requires_current_owner_and_project_access(client):
    owner = register(client)
    project = create_project(client)
    first = confirm(client, project["id"])
    member = register(client, "memory-viewer@example.com")
    member_id, project_id = UUID(member["user"]["id"]), UUID(project["id"])
    assert client.get(preference_url(project_id)).status_code == 404
    assert client.post(preference_url(project_id), json=change_body()).status_code == 404
    with client.app.state.session_factory.begin() as db:
        db.add(ProjectMember(project_id=project_id, user_id=member_id, role="viewer"))
    assert client.get(preference_url(project_id)).json() == {"preferences": [first]}
    for role in ("viewer", "editor"):
        with client.app.state.session_factory.begin() as db:
            row = db.scalar(select(ProjectMember).where(ProjectMember.user_id == member_id))
            row.role = role
        response = client.post(preference_url(project_id), json=change_body(expected_revision=1))
        assert response.status_code == 403, response.text
    owner_id = owner["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        db.get(User, UUID(owner_id)).disabled = True
    with pytest.raises(DomainError) as caught, client.app.state.session_factory.begin() as db:
        confirm_preference(
            db,
            project_id,
            owner_id,
            PreferenceChange.model_validate(change_body(expected_revision=1)),
        )
    assert caught.value.code == "FORBIDDEN"


def test_preference_sources_are_exact_and_cannot_cross_projects(client, episode):
    saved = confirm(client, episode["project_id"])
    other = create_project(client)
    for project_id, source in (
        (UUID(other["id"]), preference_source(saved)),
        (UUID(episode["project_id"]), preference_source(saved) | {"revision": 99}),
        (UUID(episode["project_id"]), preference_source(saved) | {"sha256": "f" * 64}),
    ):
        with pytest.raises(DomainError) as caught, client.app.state.session_factory() as db:
            validate_preference_source(db, project_id, source)
        assert caught.value.code == "CONTEXT_REFERENCE_INVALID"


@pytest.mark.parametrize("operation", ["update", "delete", "replace", "truncate"])
def test_preference_history_is_database_immutable_and_blocks_downgrade(
    client, episode, migrated, settings, operation
):
    saved = confirm(client, episode["project_id"])
    engine = client.app.state.engine
    if operation == "replace" and engine.dialect.name != "sqlite":
        pytest.skip("SQLite replace; PostgreSQL truncate has a separate guard")
    if operation == "truncate" and engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL truncate; SQLite replace has a separate guard")
    with pytest.raises(DBAPIError), engine.begin() as db:
        if operation == "update":
            db.execute(update(AgentPreference).values(value="被篡改"))
        elif operation == "delete":
            db.execute(delete(AgentPreference))
        elif operation == "truncate":
            db.execute(text("TRUNCATE TABLE agent_preferences"))
        else:
            db.execute(
                text("INSERT OR REPLACE INTO agent_preferences SELECT * FROM agent_preferences")
            )
    assert client.get(preference_url(episode["project_id"])).json()["preferences"] == [saved]
    with pytest.raises(RuntimeError, match="preference history"):
        command.downgrade(migrated, "0046")


@pytest.mark.parametrize("dialect", ["sqlite:///unused.db", "postgresql+psycopg://unused/unused"])
def test_preference_offline_upgrade_is_frozen_and_downgrade_requires_history_check(dialect):
    output = StringIO()
    config = Config("alembic.ini", output_buffer=output)
    config.attributes["database_url"] = dialect
    command.upgrade(config, "0046:0047", sql=True)
    assert "CREATE TABLE agent_preferences" in output.getvalue()
    assert "IMMUTABLE_HISTORY" in output.getvalue()
    with pytest.raises(RuntimeError, match="online history"):
        command.downgrade(config, "0047:0046", sql=True)


@pytest.mark.parametrize("same_key", [False, True])
def test_postgres_preference_confirmation_serializes_cas_and_idempotency(
    client, episode, settings, same_key
):
    require_postgres(settings)
    barrier = Barrier(2)
    factory = client.app.state.session_factory
    first = change_body()
    bodies = [first, first if same_key else change_body(value="另一偏好")]

    def save(body):
        barrier.wait(timeout=10)
        try:
            with factory.begin() as db:
                _, created = confirm_preference(
                    db,
                    UUID(episode["project_id"]),
                    "service-owner",
                    PreferenceChange.model_validate(body),
                )
                return "created" if created else "replay"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, bodies))
    assert sorted(results) == sorted(
        ["created", "replay" if same_key else "AGENT_PREFERENCE_CONFLICT"]
    )
    assert len(client.get(preference_url(episode["project_id"])).json()["preferences"]) == 1


def test_postgres_startup_freeze_shares_the_preference_write_gate(
    client, episode, settings, monkeypatch
):
    require_postgres(settings)
    first = confirm(client, episode["project_id"])
    entered, release = Event(), Event()
    original = context_builder.snapshot_preferences

    def capture(*args):
        rows = original(*args)
        entered.set()
        assert release.wait(timeout=10)
        return rows

    monkeypatch.setattr(context_builder, "snapshot_preferences", capture)
    factory = client.app.state.session_factory

    def start():
        with factory.begin() as db:
            return start_run(
                db,
                UUID(episode["id"]),
                "service-owner",
                "隔离故事",
                uuid4().hex,
                uuid4().hex,
                settings=settings,
            ).id

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(start)
        assert entered.wait(timeout=10)
        try:
            with pytest.raises(DBAPIError), factory.begin() as db:
                db.execute(text("SET LOCAL lock_timeout = '200ms'"))
                confirm_preference(
                    db,
                    UUID(episode["project_id"]),
                    "service-owner",
                    PreferenceChange.model_validate(
                        change_body(expected_revision=1, value="第二版")
                    ),
                )
        finally:
            release.set()
        run_id = future.result(timeout=10)
    confirm(client, episode["project_id"], expected_revision=1, value="第二版")
    response = client.get(f"/api/v1/runs/{run_id}/contexts/script/memory")
    assert response.status_code == 200, response.text
    assert response.json()["observation"]["task_context"]["required"]["preferences"] == [first]
