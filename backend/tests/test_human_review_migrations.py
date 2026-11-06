"""Database guarantees and lossless upgrade of pre-existing text review history."""

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from test_api_workflow import advance, review, start
from test_human_reviews import decide, pending_review, resume

from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import OutboxEvent


def test_0049_roundtrip_keeps_existing_workflow_outbox(client, episode, migrated):
    start(client, episode)
    with client.app.state.session_factory() as db:
        saved = db.scalar(select(OutboxEvent))
        identity, run_id, revision = saved.id, saved.run_id, saved.revision
    command.downgrade(migrated, "0048")
    assert "human_review_requests" not in inspect(client.app.state.engine).get_table_names()
    command.upgrade(migrated, "head")
    command.check(migrated)
    with client.app.state.session_factory() as db:
        saved = db.get(OutboxEvent, identity)
        assert (saved.run_id, saved.revision) == (run_id, revision)


def test_upgrade_resumes_pre_journal_text_pause_without_changing_original_identity(
    client, episode, settings, migrated, monkeypatch
):
    from arclight.application import text_reviews

    command.downgrade(migrated, "0048")
    with monkeypatch.context() as old_writer:
        old_writer.setattr(text_reviews, "pause_for_run", lambda db, run: None)
        run_id = start(client, episode).json()["run_id"]
        state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW"
    original = dict(state["pending_review"])
    command.upgrade(migrated, "head")
    assert client.get(f"/api/v1/runs/{run_id}").json()["pending_review"] == original
    assert review(client, state).status_code == 202
    with client.app.state.session_factory() as db:
        request = db.scalar(select(HumanReviewRequest))
        assert str(request.run_id) == request.snapshot_json["thread_id"] == run_id
        assert request.snapshot_json["version_id"] == original["version_id"]
        assert db.scalar(select(HumanReviewDecision)).request_id == request.id
        assert db.scalar(select(HumanReviewResume)).request_id == request.id


def test_review_history_blocks_entire_downgrade_without_partial_loss(
    client, episode, settings, migrated
):
    _, row = pending_review(client, episode, settings)
    saved = decide(client, row).json()
    assert resume(client, row, saved).status_code == 200
    before = client.get(f"/api/v1/human-reviews/{row['id']}").json()
    with pytest.raises(RuntimeError, match="Human review history exists"):
        command.downgrade(migrated, "0047")
    with client.app.state.engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0049"
    assert client.get(f"/api/v1/human-reviews/{row['id']}").json() == before


@pytest.mark.parametrize("model", [HumanReviewRequest, HumanReviewDecision, HumanReviewResume])
@pytest.mark.parametrize("operation", ["update", "delete", "replace", "truncate"])
def test_review_identity_opinion_and_receipt_are_immutable(
    client, episode, settings, model, operation
):
    _, row = pending_review(client, episode, settings)
    saved = decide(client, row).json()
    assert resume(client, row, saved).status_code == 200
    factory = client.app.state.session_factory
    dialect = client.app.state.engine.dialect.name
    if operation == "truncate" and dialect != "postgresql":
        pytest.skip("TRUNCATE is PostgreSQL-specific")
    if operation == "replace" and dialect != "sqlite":
        pytest.skip("OR REPLACE is SQLite-specific")
    with factory() as db:
        item = db.scalar(select(model))
        values = {column.name: getattr(item, column.name) for column in model.__table__.columns}
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        if operation == "update":
            db.execute(update(model).where(model.id == item.id).values(updated_at=item.updated_at))
        elif operation == "delete":
            db.execute(delete(model).where(model.id == item.id))
        elif operation == "replace":
            db.execute(model.__table__.insert().prefix_with("OR REPLACE").values(**values))
        else:
            db.execute(text(f"TRUNCATE {model.__tablename__} CASCADE"))
    with factory() as db:
        preserved = db.get(model, item.id)
        assert all(getattr(preserved, key) == value for key, value in values.items())


def test_offline_migration_has_both_database_guards_and_refuses_unchecked_downgrade():
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0048:0049", sql=True)
    sql = output.getvalue()
    for table in ("human_review_requests", "human_review_decisions", "human_review_resumes"):
        assert f"CREATE TABLE {table}" in sql
        assert f"BEFORE TRUNCATE ON {table}" in sql
    assert "IMMUTABLE_HISTORY" in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0049:0048", sql=True)
