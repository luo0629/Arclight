"""REVIEW notification migration retains old Outbox history and immutable guards."""

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from test_production_events import consume, media

from arclight.persistence.models import OutboxEvent
from arclight.persistence.production_events import ProductionEvent, ProductionEventReceipt


def snapshot(factory):
    with factory() as db:
        return {
            model.__tablename__: [
                {column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in db.scalars(select(model).order_by(model.id))
            ]
            for model in (ProductionEvent, ProductionEventReceipt, OutboxEvent)
        }


def test_0053_roundtrip_preserves_old_events_receipts_outbox_and_guards(client, episode, migrated):
    _, identity = media(client, episode)
    consume(client, identity)
    factory = client.app.state.session_factory
    original = snapshot(factory)
    command.downgrade(migrated, "0052")
    assert snapshot(factory) == original
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert snapshot(factory) == original
    checks = inspect(client.app.state.engine).get_check_constraints("production_events")
    assert any("REVIEW" in check["sqltext"] for check in checks)
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        db.execute(
            update(ProductionEvent).where(ProductionEvent.id == identity).values(source_revision=99)
        )
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        db.execute(text("DELETE FROM production_event_receipts"))
    if client.app.state.engine.dialect.name == "sqlite":
        values = original["production_events"][0]
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            db.execute(
                ProductionEvent.__table__.insert().prefix_with("OR REPLACE").values(**values)
            )


def test_0053_refuses_downgrade_with_review_event_and_keeps_entire_schema(
    client, episode, migrated
):
    _, identity = media(client, episode)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        original = db.get(ProductionEvent, identity)
        db.add(
            ProductionEvent(
                project_id=original.project_id,
                episode_id=original.episode_id,
                source_kind="REVIEW",
                source_id=identity,
                source_revision=1,
                request_sha256="f" * 64,
                event_json={"migration_fixture": True},
            )
        )
    original = snapshot(factory)
    original_tables = set(inspect(client.app.state.engine).get_table_names())
    with factory() as db:
        original_revision = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="routing history"):
        command.downgrade(migrated, "0052")
    assert snapshot(factory) == original
    with factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == original_revision
    assert set(inspect(client.app.state.engine).get_table_names()) == original_tables
    command.check(migrated)


def test_0053_postgres_offline_constraint_and_online_downgrade_guard():
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0052:0053", sql=True)
    sql = output.getvalue()
    assert "DROP CONSTRAINT ck_production_events_production_event_kind" in sql
    assert "CHECK (source_kind IN ('TEXT', 'MEDIA', 'REVIEW'))" in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0053:0052", sql=True)
