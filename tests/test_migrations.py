import pytest
from alembic import command
from arclight.persistence.base import Base
from arclight.persistence.session import build_engine
from sqlalchemy import inspect


def test_migration_round_trip(settings, migrated):
    engine = build_engine(settings)
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    command.check(migrated)
    command.downgrade(migrated, "base")
    remaining = set(inspect(engine).get_table_names())
    assert not (set(Base.metadata.tables) & remaining)
    assert "alembic_version" in remaining
    command.upgrade(migrated, "head")
    command.check(migrated)
    engine.dispose()


def test_production_rejects_insecure_settings():
    from arclight.core.config import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, environment="production")


def test_postgres_offline_sql_contains_circular_fk():
    from io import StringIO

    from alembic.config import Config

    output = StringIO()
    config = Config("alembic.ini", output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "ALTER TABLE visual_assets ADD CONSTRAINT" in sql
    assert "JSONB" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql


def test_upgrade_preserves_0001_rows_and_backfills_new_columns(settings):
    from datetime import UTC, datetime
    from uuid import uuid4

    from alembic.config import Config
    from sqlalchemy import MetaData, select

    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "0001")
    engine = build_engine(settings)
    original = MetaData()
    original.reflect(engine)
    project_id, asset_id, version_id = (uuid4() for _ in range(3))

    def key(value):
        return value if engine.dialect.name == "postgresql" else value.hex

    stamp = {"created_at": datetime.now(UTC), "updated_at": datetime.now(UTC)}
    with engine.begin() as db:
        db.execute(
            original.tables["projects"]
            .insert()
            .values(
                id=key(project_id),
                name="旧项目",
                owner_id="service-owner",
                style_guide={},
                output_profile={},
                budget_limit=0,
                **stamp,
            )
        )
        db.execute(
            original.tables["visual_assets"]
            .insert()
            .values(
                id=key(asset_id),
                project_id=key(project_id),
                asset_type="CHARACTER",
                name="旧角色",
                status="DRAFT",
                lock_version=0,
                **stamp,
            )
        )
        db.execute(
            original.tables["asset_versions"]
            .insert()
            .values(
                id=key(version_id),
                asset_id=key(asset_id),
                version_no=1,
                object_key="old/reference.png",
                checksum="a" * 64,
                metadata_json={},
                created_by="service-owner",
                **stamp,
            )
        )
    command.upgrade(config, "head")
    current = MetaData()
    current.reflect(engine)
    with engine.connect() as db:
        project = db.execute(select(current.tables["projects"])).mappings().one()
        version = db.execute(select(current.tables["asset_versions"])).mappings().one()
        assert project["name"] == "旧项目" and project["archived"] is False
        assert version["object_key"] == "old/reference.png"
        assert version["status"] == "DRAFT" and version["stale"] is False
    command.check(config)
    engine.dispose()


def test_downgrade_refuses_to_discard_manual_content(client, episode, migrated):
    from arclight.providers.text import MockTextProvider
    from sqlalchemy import text

    response = client.post(
        f"/api/v1/episodes/{episode['id']}/scripts",
        json={
            "expected_version_no": 0,
            "content": MockTextProvider().script("故事", {}).model_dump(),
        },
    )
    assert response.status_code == 201
    with pytest.raises(RuntimeError, match="无法无损降级"):
        command.downgrade(migrated, "0001")
    with client.app.state.engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
    assert (
        client.get(f"/api/v1/episodes/{episode['id']}/scripts").json()[0]["id"]
        == response.json()["id"]
    )
