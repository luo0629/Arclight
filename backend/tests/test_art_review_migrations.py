"""0050 protects immutable exact history on SQLite and real PostgreSQL."""

from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from test_art_review_workflow import decide, save, submit

from arclight.persistence.art_proposals import (
    ArtApprovedVersion,
    ArtProposalReview,
    ArtProposalVersion,
)
from arclight.persistence.models import Project


def test_empty_0050_roundtrip_preserves_existing_project(client, episode, migrated):
    with client.app.state.session_factory() as db:
        project = db.scalar(select(Project))
        identity, name = project.id, project.name
    command.downgrade(migrated, "0049")
    assert "art_proposals" not in inspect(client.app.state.engine).get_table_names()
    command.upgrade(migrated, "head")
    command.check(migrated)
    with client.app.state.session_factory() as db:
        assert db.get(Project, identity).name == name


def test_any_art_history_refuses_downgrade_without_discarding_existing_rows(
    client, episode, migrated
):
    saved, _ = save(client, episode)
    with pytest.raises(RuntimeError, match="Art proposal history exists"):
        command.downgrade(migrated, "0048")
    with client.app.state.engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0050"
    assert (
        client.get(f"/api/v1/projects/{episode['project_id']}/art-proposals/{saved['id']}").json()
        == saved
    )


@pytest.mark.parametrize("model", [ArtProposalVersion, ArtProposalReview, ArtApprovedVersion])
@pytest.mark.parametrize("operation", ["update", "delete", "replace", "truncate"])
def test_art_version_selection_and_output_history_are_database_immutable(
    client, episode, model, operation
):
    dialect = client.app.state.engine.dialect.name
    if operation == "truncate" and dialect != "postgresql":
        pytest.skip("PostgreSQL-specific")
    if operation == "replace" and dialect != "sqlite":
        pytest.skip("SQLite-specific")
    saved, _ = save(client, episode)
    waiting = submit(client, saved).json()
    assert decide(client, waiting).status_code == 200
    factory = client.app.state.session_factory
    with factory() as db:
        item = db.scalar(select(model))
        values = {column.name: getattr(item, column.name) for column in model.__table__.columns}
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        if operation == "update":
            db.execute(update(model).where(model.id == item.id).values(updated_at=item.updated_at))
        elif operation == "delete":
            db.execute(delete(model).where(model.id == item.id))
        elif operation == "truncate":
            db.execute(text(f"TRUNCATE {model.__tablename__} CASCADE"))
        else:
            # Exercise secondary unique keys as well as the primary identity.
            replaced = {**values, "id": uuid4()}
            db.execute(model.__table__.insert().prefix_with("OR REPLACE").values(**replaced))
    with factory() as db:
        assert all(getattr(db.get(model, item.id), key) == value for key, value in values.items())


def test_offline_0050_contains_postgres_guards_and_requires_online_downgrade():
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0049:0050", sql=True)
    sql = output.getvalue()
    for table in ("art_proposal_versions", "art_proposal_reviews", "art_approved_versions"):
        assert f"CREATE TABLE {table}" in sql and f"BEFORE TRUNCATE ON {table}" in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0050:0049", sql=True)
