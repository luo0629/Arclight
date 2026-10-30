"""GEN-01: real migrated schema, constraints, indexes and preservation."""

from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from arclight.persistence.base import utcnow
from arclight.persistence.models import (
    DependencyManifest,
    GenerationAttempt,
    GenerationJob,
    VisualAsset,
)


@pytest.fixture
def generation_seed(client, episode):
    factory = client.app.state.session_factory
    with factory.begin() as db:
        asset = VisualAsset(
            project_id=UUID(episode["project_id"]), asset_type="CHARACTER", name="测试目标"
        )
        db.add(asset)
        db.flush()
        manifest = DependencyManifest(
            project_id=asset.project_id,
            target_asset_id=asset.id,
            creation_key=uuid4(),
            request_fingerprint="a" * 64,
            snapshot_json={"inputs": [], "context": {}},
            created_by="service-owner",
        )
        db.add(manifest)
        db.flush()
        return {
            "project_id": asset.project_id,
            "episode_id": UUID(episode["id"]),
            "target_asset_id": asset.id,
            "input_manifest_id": manifest.id,
            "target_type": "CHARACTER",
            "task_type": "IMAGE",
            "provider": "fake",
            "model": "fake-image",
            "idempotency_key": str(uuid4()),
            "request_fingerprint": "a" * 64,
            "created_by": "service-owner",
        }


def add_job(db, values, **changes):
    row = GenerationJob(**(values | changes))
    db.add(row)
    db.flush()
    return row


def add_attempt(db, job, **changes):
    values = {
        "job_id": job.id,
        "attempt_no": 1,
        "input_manifest_id": job.input_manifest_id,
        "request_fingerprint": job.request_fingerprint,
        "provider": job.provider,
        "model": job.model,
        "input_snapshot_json": {"prompt": "测试"},
        "submitted_at": utcnow(),
    }
    row = GenerationAttempt(**(values | changes))
    db.add(row)
    db.flush()
    return row


@pytest.mark.parametrize(
    "target,task",
    [("CHARACTER", "IMAGE"), ("SCENE", "IMAGE"), ("AUDIO", "AUDIO"), ("RENDER", "RENDER")],
)
def test_generic_targets_do_not_require_shots(client, generation_seed, target, task):
    with client.app.state.session_factory.begin() as db:
        row = add_job(db, generation_seed, target_type=target, task_type=task, target_asset_id=None)
        first = add_attempt(db, row)
        second = add_attempt(db, row, attempt_no=2)
        assert first.id != second.id
        assert row.target_asset_id is None
    indexes = {
        item["name"] for item in inspect(client.app.state.engine).get_indexes("generation_jobs")
    }
    assert {
        "ix_generation_jobs_status_created",
        "ix_generation_jobs_project_id",
        "ix_generation_jobs_target_asset_id",
    } <= indexes


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "QUEUED"},
        {"attempt_count": -1},
        {"revision": -1},
        {"target_type": "UNKNOWN"},
        {"task_type": "TEXT"},
        {"input_manifest_id": uuid4()},
        {"episode_id": uuid4()},
        {"target_asset_id": uuid4()},
        {"expected_lock_version": -1},
    ],
)
def test_job_database_constraints(client, generation_seed, changes):
    with pytest.raises(IntegrityError), client.app.state.session_factory.begin() as db:
        add_job(db, generation_seed, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt_no": 0},
        {"status": "QUEUED"},
        {"latency_ms": -1},
        {"cost": -1},
        {"input_manifest_id": uuid4()},
        {"job_id": uuid4()},
    ],
)
def test_attempt_database_constraints(client, generation_seed, changes):
    with pytest.raises(IntegrityError), client.app.state.session_factory.begin() as db:
        row = add_job(db, generation_seed)
        add_attempt(db, row, **changes)


@pytest.mark.parametrize("kind", ["job_key", "attempt_no", "provider_id"])
def test_unique_execution_identities(client, generation_seed, kind):
    with pytest.raises(IntegrityError), client.app.state.session_factory.begin() as db:
        row = add_job(db, generation_seed)
        if kind == "job_key":
            add_job(db, generation_seed)
        else:
            add_attempt(db, row, provider_request_id="fake-request")
            add_attempt(
                db,
                row,
                attempt_no=1 if kind == "attempt_no" else 2,
                provider_request_id="fake-request",
            )


def test_downgrade_preserves_media_history(client, generation_seed, migrated):
    with client.app.state.session_factory.begin() as db:
        row = add_job(db, generation_seed)
        add_attempt(db, row)
        job_id = row.id
    with pytest.raises(RuntimeError, match="Generation history exists"):
        command.downgrade(migrated, "0026")
    with client.app.state.session_factory() as db:
        assert db.scalar(select(GenerationJob.id)) == job_id
        assert db.scalar(select(GenerationAttempt.attempt_no)) == 1
    command.check(migrated)


def test_generation_postgres_offline_ddl(migrated):
    output = StringIO()
    config = Config(migrated.config_file_name, output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0026:0027", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE generation_jobs" in sql and "CREATE TABLE generation_attempts" in sql
    assert "JSONB" in sql and "uq_generation_provider_request" in sql
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0027:0026", sql=True)
