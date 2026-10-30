"""GEN-05 migrations preserve old jobs and enforce durable delivery constraints."""

from io import StringIO
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from test_generation_delivery import Provider, deliver, event_for
from test_generation_jobs import capture_ok
from test_generation_schema import add_job
from test_generation_schema import generation_seed as generation_seed

from arclight.persistence.base import Base
from arclight.persistence.generation import GenerationDispatch
from arclight.persistence.models import GenerationJob


def test_duplicate_active_asset_upgrade_refuses_before_any_ddl(client, generation_seed, migrated):
    command.downgrade(migrated, "0030")
    factory = client.app.state.session_factory
    with factory.begin() as db:
        add_job(db, generation_seed)
        add_job(db, generation_seed, idempotency_key="second-command")
    with pytest.raises(RuntimeError, match="Concurrent active generation exists"):
        command.upgrade(migrated, "head")
    engine = client.app.state.engine
    assert "generation_dispatches" not in inspect(engine).get_table_names()
    assert "uq_generation_active_asset" not in {
        index["name"] for index in inspect(engine).get_indexes("generation_jobs")
    }
    with factory.begin() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0030"
        assert len(list(db.scalars(select(GenerationJob)))) == 2
        # Explicit, definite terminal resolution of the legacy duplicate, not a migration rewrite.
        rows = list(db.scalars(select(GenerationJob)))
        rows[1].status = "FAILED"
    command.upgrade(migrated, "head")
    command.check(migrated)
    with factory() as db:
        assert db.scalar(select(GenerationDispatch)) is None
        assert len(list(db.scalars(select(GenerationJob)))) == 2


@pytest.mark.parametrize(
    "status",
    [
        "CREATED",
        "SUBMITTING",
        "SUBMITTED",
        "RUNNING",
        "RETRY_WAIT",
        "CANCEL_REQUESTED",
        "RESULT_UNKNOWN",
        "RECONCILING",
        "MANUAL_REVIEW",
    ],
)
def test_database_rejects_two_active_jobs_for_same_asset(client, generation_seed, status):
    factory = client.app.state.session_factory
    with factory.begin() as db:
        add_job(db, generation_seed, status=status)
    with pytest.raises(DBAPIError), factory.begin() as db:
        add_job(db, generation_seed, idempotency_key="another-command")


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_definite_terminal_job_releases_asset_mutex(client, generation_seed, status):
    with client.app.state.session_factory.begin() as db:
        add_job(db, generation_seed, status=status)
        add_job(db, generation_seed, idempotency_key="another-command")


def test_final_dispatch_receipt_is_immutable_and_downgrade_preserves_head(
    client, episode, migrated
):
    job = capture_ok(client, episode["project_id"])
    assert deliver(client, job, Provider()) == "SUCCEEDED"
    factory = client.app.state.session_factory
    with factory() as db:
        before = dict(db.execute(select(GenerationDispatch.__table__)).mappings().one())
    for sql in (
        "UPDATE generation_dispatches SET receipt_json = '{}'",
        "UPDATE generation_dispatches SET finished_at = NULL",
        "UPDATE generation_dispatches SET provider_supports_idempotency = false",
        "DELETE FROM generation_dispatches",
    ):
        with pytest.raises(DBAPIError), factory.begin() as db:
            db.execute(text(sql))
    with pytest.raises(RuntimeError, match="Generation history exists"):
        command.downgrade(migrated, "0030")
    command.check(migrated)
    with factory() as db:
        assert dict(db.execute(select(GenerationDispatch.__table__)).mappings().one()) == before
        assert db.get(GenerationDispatch, event_for(client, job)).status == "FINISHED"


def test_dispatch_revision_and_claim_constraints_reject_invalid_rows(client, episode):
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    with factory() as db:
        row = dict(db.execute(select(GenerationDispatch.__table__)).mappings().one())
    for changes in (
        {"expected_revision": -1},
        {"status": "UNKNOWN"},
        {"status": "CLAIMED"},
        {"status": "FINISHED"},
        {"provider_idempotency_key": "unsupported"},
    ):
        candidate = row | {"id": uuid4(), "expected_revision": 10} | changes
        with pytest.raises(DBAPIError), factory.begin() as db:
            db.execute(Base.metadata.tables["generation_dispatches"].insert().values(**candidate))
    assert job["status"] == "CREATED"


def test_postgres_offline_dispatch_migration_has_guards_and_active_mutex(migrated):
    output = StringIO()
    migrated.output_buffer = output
    migrated.attributes["database_url"] = "postgresql+psycopg://test:test@localhost/test"
    command.upgrade(migrated, "0030:0031", sql=True)
    ddl = output.getvalue()
    for required in (
        "uq_generation_active_asset",
        "HAVING COUNT(*) > 1",
        "generation_dispatches",
        "immutable_generation_dispatches_guard",
        "BEFORE TRUNCATE",
        "provider_idempotency_key",
    ):
        assert required in ddl
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(migrated, "0031:0030", sql=True)
