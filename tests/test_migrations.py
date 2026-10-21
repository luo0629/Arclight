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
