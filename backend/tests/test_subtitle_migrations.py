"""Subtitle production is append-only and refuses destructive downgrade."""

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from test_subtitle_production import (
    render_case,
    review_case,
    subtitle_case,
    subtitle_source,
    timeline_case,
)

__all__ = ["render_case", "review_case", "timeline_case", "subtitle_source", "subtitle_case"]


def test_subtitle_empty_roundtrip_matches_metadata(client, migrated):
    engine = client.app.state.session_factory.kw["bind"]
    command.check(migrated)
    command.downgrade(migrated, "0065")
    assert "subtitle_productions" not in inspect(engine).get_table_names()
    command.upgrade(migrated, "head")
    command.check(migrated)


def test_subtitle_history_blocks_downgrade_before_ddl(client, subtitle_case, migrated):
    engine = client.app.state.session_factory.kw["bind"]
    with engine.connect() as db:
        before = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot discard subtitle production history"):
        command.downgrade(migrated, "0065")
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == before
        assert db.scalar(text("SELECT count(*) FROM subtitle_productions")) == 1
    assert "subtitle_productions" in inspect(engine).get_table_names()


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_subtitle_history_cannot_be_rewritten(client, subtitle_case, operation):
    engine = client.app.state.session_factory.kw["bind"]
    if operation == "UPDATE":
        sql = "UPDATE subtitle_productions SET request_fingerprint='changed'"
    elif operation == "DELETE":
        sql = "DELETE FROM subtitle_productions"
    elif engine.dialect.name == "sqlite":
        sql = "INSERT OR REPLACE INTO subtitle_productions SELECT * FROM subtitle_productions"
    else:
        sql = "TRUNCATE subtitle_productions"
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
        db.execute(text(sql))
    with engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM subtitle_productions")) == 1


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_subtitle_offline_sql_has_scope_and_immutable_guards(dialect):
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = (
        "sqlite:///unused.db"
        if dialect == "sqlite"
        else "postgresql+psycopg://unused:unused@localhost/unused"
    )
    command.upgrade(config, "0065:0066", sql=True)
    sql = output.getvalue()
    assert "immutable_subtitle_productions_insert" in sql
    assert "REFERENCES timelines" in sql
    assert "REFERENCES stored_files" in sql
    assert "dependency_outputs" in sql
    assert "m.target_asset_id=a.id" in sql
    assert "subtitle_production_id" in sql
    if dialect == "postgresql":
        assert "BEFORE TRUNCATE" in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0066:0065", sql=True)
