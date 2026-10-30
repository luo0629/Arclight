"""GEN-03: enforce history outside the ORM and preserve existing migrations."""

from decimal import Decimal
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from test_generation_attempts import fail, start
from test_generation_jobs import capture_ok
from test_generation_schema import add_attempt, add_job
from test_generation_schema import generation_seed as generation_seed
from test_immutable_history import changed

from arclight.persistence.base import Base
from arclight.persistence.models import DependencyManifest, GenerationAttempt

JOB_MUTABLE = {
    "updated_at",
    "status",
    "revision",
    "attempt_count",
    "provider_request_id",
    "cancel_requested_at",
}
ATTEMPT_MUTABLE = {
    "updated_at",
    "status",
    "provider_request_id",
    "result_json",
    "error_code",
    "latency_ms",
    "cost",
    "finished_at",
}


@pytest.mark.parametrize("finished", [False, True])
def test_sql_guards_preserve_identity_snapshots_and_finished_outcomes(client, episode, finished):
    job = capture_ok(client, episode["project_id"])
    attempt = start(client, job)
    if finished:
        fail(client, job, attempt)
    engine = client.app.state.engine
    for name, allowed in (
        ("generation_jobs", JOB_MUTABLE),
        ("generation_attempts", set() if finished else ATTEMPT_MUTABLE),
    ):
        table = Base.metadata.tables[name]
        with engine.connect() as db:
            before = dict(db.execute(select(table)).mappings().one())
        for column in table.columns:
            if column.name in allowed:
                continue
            patch = {
                "updated_at": before["updated_at"],
                column.name: Decimal("1.000001")
                if column.name == "cost"
                else changed(before[column.name], column),
            }
            with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
                db.execute(table.update().values(**patch))
        with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.execute(table.delete())
        if engine.dialect.name == "postgresql":
            with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
                db.exec_driver_sql(f'TRUNCATE TABLE "{name}" CASCADE')
        else:
            for values in (before, before | {"id": uuid4()}):
                with pytest.raises(IntegrityError, match="replacement"), engine.begin() as db:
                    db.execute(table.insert().prefix_with("OR REPLACE").values(**values))
        with engine.connect() as db:
            assert dict(db.execute(select(table)).mappings().one()) == before


def test_provider_request_id_is_write_once_before_completion(client, episode):
    job = capture_ok(client, episode["project_id"])
    attempt = start(client, job)
    table = GenerationAttempt.__table__
    engine = client.app.state.engine
    with engine.begin() as db:
        db.execute(
            table.update()
            .where(table.c.id == attempt.id)
            .values(provider_request_id="mock:accepted")
        )
    for value in (None, "mock:rebound"):
        with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.execute(
                table.update().where(table.c.id == attempt.id).values(provider_request_id=value)
            )


def test_upgrade_0028_preserves_manifest_jobs_and_attempts_then_guards_history(
    client, generation_seed, migrated
):
    command.downgrade(migrated, "0028")
    factory = client.app.state.session_factory
    with factory.begin() as db:
        job = add_job(db, generation_seed)
        add_attempt(db, job, provider_request_id="legacy:request", error_code="LEGACY_ERROR")
    names = ("dependency_manifests", "generation_jobs", "generation_attempts")
    engine = client.app.state.engine
    with engine.connect() as db:
        before = {
            name: [dict(row) for row in db.execute(select(Base.metadata.tables[name])).mappings()]
            for name in names
        }
    command.upgrade(migrated, "head")
    with engine.connect() as db:
        after = {
            name: [dict(row) for row in db.execute(select(Base.metadata.tables[name])).mappings()]
            for name in names
        }
        assert after == before
        if engine.dialect.name == "sqlite":
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    assert next(
        c
        for c in inspect(engine).get_columns("dependency_manifests")
        if c["name"] == "target_asset_id"
    )["nullable"]
    with pytest.raises(RuntimeError, match="Generation history exists"):
        command.downgrade(migrated, "0028")
    command.check(migrated)


def test_nullable_manifest_without_job_also_refuses_lossy_downgrade(client, episode, migrated):
    with client.app.state.session_factory.begin() as db:
        db.add(
            DependencyManifest(
                project_id=UUID(episode["project_id"]),
                target_asset_id=None,
                creation_key=uuid4(),
                request_fingerprint="f" * 64,
                snapshot_json={"inputs": []},
                created_by="service-owner",
            )
        )
    with pytest.raises(RuntimeError, match="Generic media manifests exist"):
        command.downgrade(migrated, "0028")
    command.check(migrated)


def test_postgres_offline_ddl_contains_frozen_guards_and_nullable_target(migrated):
    output = StringIO()
    config = Config(migrated.config_file_name, output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0028:0029", sql=True)
    sql = output.getvalue()
    assert "ALTER TABLE dependency_manifests ALTER COLUMN target_asset_id DROP NOT NULL" in sql
    assert "immutable_generation_attempts_truncate" in sql and "OLD.finished_at IS NOT NULL" in sql
    assert "OLD.provider_request_id IS NOT NULL" in sql and "input_snapshot_json" in sql
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0029:0028", sql=True)
