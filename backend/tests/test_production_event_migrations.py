"""FLOW-02 migration preservation and database-enforced immutable evidence."""

from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from test_api_workflow import start
from test_production_events import consume, media

from arclight.persistence.models import OutboxEvent
from arclight.persistence.production_events import ProductionEvent, ProductionEventReceipt


def test_0048_roundtrip_retains_old_workflow_outbox(client, episode, migrated):
    run_id = UUID(start(client, episode).json()["run_id"])
    factory = client.app.state.session_factory
    with factory() as db:
        original = db.scalar(select(OutboxEvent).where(OutboxEvent.run_id == run_id))
        identity, revision = original.id, original.revision
    command.downgrade(migrated, "0047")
    assert "production_events" not in inspect(client.app.state.engine).get_table_names()
    command.upgrade(migrated, "head")
    command.check(migrated)
    with factory() as db:
        preserved = db.get(OutboxEvent, identity)
        assert preserved.run_id == run_id and preserved.revision == revision
        assert preserved.production_event_id is None and preserved.event_type == "workflow.execute"


def test_0048_with_history_refuses_destructive_downgrade(client, episode, settings, migrated):
    # Use a real media notification so the 0048 guard itself is exercised,
    # independently of the newer human-review history guard.
    _, identity = media(client, episode)
    result = consume(client, identity)
    with client.app.state.engine.connect() as connection:
        original_head = connection.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="history exists"):
        command.downgrade(migrated, "0047")
    with client.app.state.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == original_head
    assert consume(client, identity) == result


@pytest.mark.parametrize("model", [ProductionEvent, ProductionEventReceipt])
@pytest.mark.parametrize("operation", ["update", "delete", "replace", "truncate"])
def test_stage_history_cannot_be_overwritten_or_removed(client, episode, model, operation):
    _, identity = media(client, episode)
    consume(client, identity)
    factory = client.app.state.session_factory
    dialect = client.app.state.engine.dialect.name
    if operation == "truncate" and dialect != "postgresql":
        pytest.skip("TRUNCATE exists only on PostgreSQL; DELETE/REPLACE checked on SQLite")
    if operation == "replace" and dialect != "sqlite":
        pytest.skip("OR REPLACE is SQLite-specific; PostgreSQL uniqueness/UPDATE tested")
    with factory() as db:
        saved = db.scalar(select(model))
        values = {column.name: getattr(saved, column.name) for column in model.__table__.columns}
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        if operation == "update":
            db.execute(
                update(model).where(model.id == saved.id).values(updated_at=saved.updated_at)
            )
        elif operation == "delete":
            db.execute(delete(model).where(model.id == saved.id))
        elif operation == "replace":
            db.execute(model.__table__.insert().prefix_with("OR REPLACE").values(**values))
        else:
            db.execute(text(f"TRUNCATE {model.__tablename__} CASCADE"))
    with factory() as db:
        assert db.get(model, saved.id) is not None


def test_outbox_target_contract_rejects_ambiguous_or_missing_targets(client, episode):
    job, identity = media(client, episode)
    for targets in (
        {"run_id": None, "production_event_id": None},
        {"run_id": None, "production_event_id": identity},
    ):
        with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
            db.add(OutboxEvent(**targets, revision=0, event_type="workflow.execute"))
            db.flush()
    assert job["status"] == "CREATED"


def test_production_event_postgres_offline_ddl(tmp_path):
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0047:0048", sql=True)
    sql = output.getvalue()
    assert (
        "CREATE TABLE production_events" in sql and "CREATE TABLE production_event_receipts" in sql
    )
    assert "production.coordinate" in sql and "workflow.execute" in sql
    assert "BEFORE TRUNCATE" in sql and "IMMUTABLE_HISTORY" in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0048:0047", sql=True)
