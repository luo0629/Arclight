"""Migration and direct-SQL protection for immutable narrative ledger history."""

from copy import deepcopy
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, UniqueConstraint, inspect, select, text
from sqlalchemy.exc import DBAPIError
from test_story_ledger import approved_script, ledger_case, save_ledger

from arclight.persistence.story_ledger import (
    StoryLedgerApproval,
    StoryLedgerHead,
    StoryLedgerVersion,
)

__all__ = ["ledger_case"]

LEDGER_MODELS = (StoryLedgerVersion, StoryLedgerApproval, StoryLedgerHead)
LEDGER_TABLES = tuple(model.__tablename__ for model in LEDGER_MODELS)


def table_rows(db, names):
    metadata = MetaData()
    known_tables = {model.__tablename__: model.__table__ for model in LEDGER_MODELS}
    result = {}
    for name in names:
        table = known_tables.get(name)
        if table is None:
            table = Table(name, metadata, autoload_with=db)
        result[name] = [
            dict(row) for row in db.execute(select(table).order_by(table.c.id)).mappings()
        ]
    return result


def changed(value):
    if isinstance(value, UUID) or value is None:
        return uuid4()
    if isinstance(value, datetime):
        return value + timedelta(seconds=1)
    if isinstance(value, int):
        return value + 1
    if isinstance(value, dict):
        return value | {"tampered": True}
    return "tampered"


def foreign_project(client):
    response = client.post("/api/v1/projects", json={"name": "账本数据库隔离"})
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def test_0069_empty_roundtrip_and_upgrade_preserve_0068_rows(client, episode, migrated):
    engine = client.app.state.engine
    command.downgrade(migrated, "0068")
    assert set(LEDGER_TABLES).isdisjoint(inspect(engine).get_table_names())
    approved_script(client, episode)
    setting = client.post(
        f"/api/v1/projects/{episode['project_id']}/story-entries",
        json={"kind": "WORLD", "name": "0068原始世界", "rules": ["不能复活"]},
    )
    assert setting.status_code == 201, setting.text
    old_tables = (
        "projects",
        "episodes",
        "scripts",
        "story_entries",
        "asset_versions",
        "dependency_manifests",
        "reviews",
    )
    with engine.connect() as db:
        before = table_rows(db, old_tables)
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0068"
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert set(LEDGER_TABLES) <= set(inspect(engine).get_table_names())
    with engine.connect() as db:
        assert table_rows(db, old_tables) == before
        assert all(not values for values in table_rows(db, LEDGER_TABLES).values())
    command.downgrade(migrated, "0068")
    assert set(LEDGER_TABLES).isdisjoint(inspect(engine).get_table_names())
    with engine.connect() as db:
        assert table_rows(db, old_tables) == before
    command.upgrade(migrated, "head")
    command.check(migrated)
    with engine.connect() as db:
        assert table_rows(db, old_tables) == before


def test_0069_nonempty_downgrade_refuses_before_ddl_and_keeps_guards(client, ledger_case, migrated):
    engine = client.app.state.engine
    names = set(inspect(engine).get_table_names())
    with engine.connect() as db:
        before = table_rows(db, LEDGER_TABLES)
        revision = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot discard story ledger history"):
        command.downgrade(migrated, "0068")
    assert set(inspect(engine).get_table_names()) == names
    with engine.connect() as db:
        assert table_rows(db, LEDGER_TABLES) == before
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == revision
    for model in LEDGER_MODELS:
        table = model.__table__
        identity = before[model.__tablename__][0]["id"]
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.execute(table.delete().where(table.c.id == identity))
    command.check(migrated)


@pytest.mark.parametrize("model", [StoryLedgerVersion, StoryLedgerApproval])
def test_0069_history_rejects_every_field_update_and_direct_delete(client, ledger_case, model):
    engine = client.app.state.engine
    table = model.__table__
    with engine.connect() as db:
        original = dict(db.execute(select(table)).mappings().one())
    for column in table.columns:
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.execute(
                table.update()
                .where(table.c.id == original["id"])
                .values({column.name: changed(original[column.name])})
            )
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
        db.execute(table.delete().where(table.c.id == original["id"]))
    with engine.connect() as db:
        assert dict(db.execute(select(table)).mappings().one()) == original


def test_0069_sqlite_replace_cannot_bypass_any_unique_identity(client, ledger_case):
    engine = client.app.state.engine
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite OR REPLACE bypass is exercised on SQLite")
    case = ledger_case
    second = save_ledger(
        client,
        case["episode"],
        deepcopy(case["content"]),
        revision=case["approved"]["head"]["revision"],
        base_version_id=case["created"]["version"]["id"],
        key="ledger-replace-alternative-version",
    )
    alternative_version = UUID(second["version"]["id"])
    other_project = foreign_project(client)
    with engine.connect() as db:
        db.exec_driver_sql("PRAGMA recursive_triggers=OFF")
        assert db.exec_driver_sql("PRAGMA recursive_triggers").scalar() == 0
        before = table_rows(db, LEDGER_TABLES)
        db.rollback()
        for model in LEDGER_MODELS:
            table = model.__table__
            original = before[model.__tablename__][0]
            groups = [("id",)] + sorted(
                tuple(column.name for column in constraint.columns)
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)
            )
            unique_columns = set().union(*(set(group) for group in groups))
            for group in groups:
                replacement = dict(original)
                for name in unique_columns - set(group) - {"project_id"}:
                    if name == "version_id":
                        replacement[name] = alternative_version
                    elif name == "version_no":
                        replacement[name] = 10_000
                    elif name == "creation_key":
                        replacement[name] = uuid4().hex
                    else:
                        replacement[name] = uuid4()
                if model is StoryLedgerHead:
                    replacement.update(
                        revision=0,
                        latest_version_id=None,
                        approved_version_id=None,
                        approval_id=None,
                    )
                    if group == ("id",):
                        replacement["project_id"] = other_project
                with pytest.raises(
                    DBAPIError, match="IMMUTABLE_HISTORY|STORY_LEDGER_HEAD_CONFLICT"
                ):
                    db.execute(table.insert().prefix_with("OR REPLACE").values(replacement))
                db.rollback()
        assert table_rows(db, LEDGER_TABLES) == before


@pytest.mark.parametrize("table_name", LEDGER_TABLES)
def test_0069_postgres_truncate_cannot_discard_history_or_reset_head(
    client, ledger_case, table_name
):
    engine = client.app.state.engine
    if engine.dialect.name != "postgresql":
        pytest.skip("Real PostgreSQL TRUNCATE guards run in isolated PostgreSQL CI")
    with engine.connect() as db:
        before = table_rows(db, LEDGER_TABLES)
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
        db.execute(text(f"TRUNCATE TABLE {table_name} CASCADE"))
    with engine.connect() as db:
        assert table_rows(db, LEDGER_TABLES) == before


def test_0069_head_revision_and_project_scope_are_protected_in_sql(client, ledger_case):
    engine = client.app.state.engine
    other_project = foreign_project(client)
    with engine.connect() as db:
        version = dict(db.execute(select(StoryLedgerVersion.__table__)).mappings().one())
        approval = dict(db.execute(select(StoryLedgerApproval.__table__)).mappings().one())
        head = dict(db.execute(select(StoryLedgerHead.__table__)).mappings().one())
    foreign_version = dict(version) | {
        "id": uuid4(),
        "project_id": other_project,
        "creation_key": uuid4().hex,
    }
    with engine.begin() as db:
        db.execute(StoryLedgerVersion.__table__.insert().values(foreign_version))
    attempts = [
        (
            StoryLedgerVersion.__table__.insert().values(
                dict(foreign_version)
                | {
                    "id": uuid4(),
                    "version_no": 2,
                    "creation_key": uuid4().hex,
                    "base_version_id": version["id"],
                }
            ),
            "IMMUTABLE_HISTORY",
        ),
        (
            StoryLedgerApproval.__table__.insert().values(
                dict(approval)
                | {
                    "id": uuid4(),
                    "project_id": other_project,
                    "creation_key": uuid4().hex,
                    "version_id": foreign_version["id"],
                    "content_sha256": "f" * 64,
                }
            ),
            "IMMUTABLE_HISTORY",
        ),
        (
            StoryLedgerApproval.__table__.insert().values(
                dict(approval)
                | {"id": uuid4(), "project_id": other_project, "creation_key": uuid4().hex}
            ),
            "IMMUTABLE_HISTORY",
        ),
    ]
    for statement, message in attempts:
        with pytest.raises(DBAPIError, match=message), engine.begin() as db:
            db.execute(statement)
    for changes in (
        {"revision": head["revision"]},
        {"revision": head["revision"] + 2},
        {"revision": head["revision"] + 1, "latest_version_id": foreign_version["id"]},
        {"revision": head["revision"] + 1, "approved_version_id": foreign_version["id"]},
        {"revision": head["revision"] + 1, "approval_id": None},
        {"revision": head["revision"] + 1, "project_id": other_project},
        {"revision": head["revision"] + 1, "id": uuid4()},
    ):
        with pytest.raises(DBAPIError, match="STORY_LEDGER_HEAD_CONFLICT"), engine.begin() as db:
            db.execute(
                StoryLedgerHead.__table__.update()
                .where(StoryLedgerHead.__table__.c.id == head["id"])
                .values(changes)
            )
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
        db.execute(StoryLedgerHead.__table__.delete())
    with engine.connect() as db:
        assert dict(db.execute(select(StoryLedgerHead.__table__)).mappings().one()) == head


def test_0069_offline_postgres_guards_and_offline_downgrade_refusal():
    output = StringIO()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://offline:offline@localhost/offline"
    command.upgrade(config, "0068:0069", sql=True)
    sql = output.getvalue()
    for table_name in LEDGER_TABLES:
        assert f"CREATE TABLE {table_name}" in sql
    for table_name in ("story_ledger_versions", "story_ledger_approvals"):
        assert f"CREATE TRIGGER immutable_{table_name}_write BEFORE UPDATE OR DELETE" in sql
        assert f"CREATE TRIGGER immutable_{table_name}_truncate BEFORE TRUNCATE" in sql
        assert f"CREATE TRIGGER immutable_{table_name}_insert BEFORE INSERT" in sql
    assert "story_ledger_head_delete BEFORE DELETE" in sql
    assert "story_ledger_head_truncate BEFORE TRUNCATE" in sql
    assert "NEW.revision<>OLD.revision+1" in sql
    assert "v.project_id=NEW.project_id" in sql
    assert "v.content_sha256=NEW.content_sha256" in sql
    assert "v.source_manifest_sha256=NEW.source_manifest_sha256" in sql
    assert "uq_story_ledger_version" in sql and "uq_story_ledger_approval_version" in sql
    with pytest.raises(RuntimeError, match="requires an online history check"):
        command.downgrade(config, "0069:0068", sql=True)
