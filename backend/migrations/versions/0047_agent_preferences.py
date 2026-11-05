"""Append-only, explicitly confirmed project preferences.

Revision ID: 0047
Revises: 0046
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_preferences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("preference_name", sa.String(40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("value", sa.Text()),
        sa.Column("confirmed_by", sa.String(100), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "project_id", "preference_name", "revision", name="uq_agent_preferences_revision"
        ),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_agent_preferences_creation"),
        sa.CheckConstraint("revision > 0", name="agent_preference_revision_positive"),
        sa.CheckConstraint(
            "preference_name IN ('narrative_tone', 'dialogue_style', 'pacing', 'visual_tone')",
            name="agent_preference_content_key",
        ),
    )
    op.create_index("ix_agent_preferences_project_id", "agent_preferences", ["project_id"])
    if op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_agent_preferences_{operation.lower()} "
                f"BEFORE {operation} ON agent_preferences "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: Agent preference'); END"
            )
        op.execute(
            "CREATE TRIGGER immutable_agent_preferences_replace BEFORE INSERT ON agent_preferences "
            "WHEN EXISTS (SELECT 1 FROM agent_preferences WHERE id = NEW.id OR "
            "(project_id = NEW.project_id AND (creation_key = NEW.creation_key OR "
            "(preference_name = NEW.preference_name AND revision = NEW.revision)))) "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: Agent preference'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION immutable_agent_preferences_guard() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: Agent preference'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_agent_preferences_write BEFORE UPDATE OR DELETE "
            "ON agent_preferences FOR EACH ROW EXECUTE FUNCTION immutable_agent_preferences_guard()"
        )
        op.execute(
            "CREATE TRIGGER immutable_agent_preferences_truncate BEFORE TRUNCATE "
            "ON agent_preferences FOR EACH STATEMENT "
            "EXECUTE FUNCTION immutable_agent_preferences_guard()"
        )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Preference downgrade requires online history preservation check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE agent_preferences IN ACCESS EXCLUSIVE MODE")
    else:
        driver = db.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            db.exec_driver_sql("BEGIN IMMEDIATE")
    if db.scalar(sa.text("SELECT 1 FROM agent_preferences LIMIT 1")):
        raise RuntimeError("Agent preference history exists; cannot discard it")
    op.drop_table("agent_preferences")
    if db.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_agent_preferences_guard()")
