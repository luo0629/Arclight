"""Immutable compare-and-append receipts for timeline saves."""

import sqlalchemy as sa
from alembic import context, op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "timeline_revisions",
        sa.Column("id", sa.Uuid(), sa.ForeignKey("timelines.id"), primary_key=True),
        sa.Column("based_on_timeline_id", sa.Uuid(), sa.ForeignKey("timelines.id"), nullable=False),
        sa.Column("expected_version_no", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("based_on_timeline_id"),
        sa.CheckConstraint("expected_version_no > 0", name="timeline_revision_positive"),
    )
    invalid = (
        "NOT EXISTS (SELECT 1 FROM timelines n JOIN timelines b "
        "ON b.episode_id=n.episode_id WHERE n.id=NEW.id AND b.id=NEW.based_on_timeline_id "
        "AND n.sealed AND b.sealed AND n.version_no=b.version_no+1 "
        "AND b.version_no=NEW.expected_version_no AND n.created_by=NEW.created_by) "
        "OR EXISTS (SELECT 1 FROM timeline_revisions WHERE id=NEW.id "
        "OR based_on_timeline_id=NEW.based_on_timeline_id)"
    )
    if op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_timeline_revisions_{operation.lower()} "
                f"BEFORE {operation} ON timeline_revisions "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: timeline revision'); END"
            )
        op.execute(
            "CREATE TRIGGER immutable_timeline_revisions_insert "
            f"BEFORE INSERT ON timeline_revisions WHEN {invalid} "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: timeline revision scope'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION immutable_timeline_revisions_guard() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: timeline revision'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_timeline_revisions_write BEFORE UPDATE OR DELETE "
            "ON timeline_revisions FOR EACH ROW "
            "EXECUTE FUNCTION immutable_timeline_revisions_guard()"
        )
        op.execute(
            "CREATE TRIGGER immutable_timeline_revisions_truncate BEFORE TRUNCATE "
            "ON timeline_revisions FOR EACH STATEMENT "
            "EXECUTE FUNCTION immutable_timeline_revisions_guard()"
        )
        op.execute(
            "CREATE FUNCTION immutable_timeline_revisions_insert_guard() RETURNS trigger "
            f"LANGUAGE plpgsql AS $$ BEGIN IF {invalid} THEN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: timeline revision scope'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_timeline_revisions_insert BEFORE INSERT "
            "ON timeline_revisions FOR EACH ROW "
            "EXECUTE FUNCTION immutable_timeline_revisions_insert_guard()"
        )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Timeline revision downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE timeline_revisions IN ACCESS EXCLUSIVE MODE")
    if db.scalar(sa.text("SELECT 1 FROM timeline_revisions LIMIT 1")):
        raise RuntimeError("Cannot discard timeline revision history")
    op.drop_table("timeline_revisions")
    if db.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_timeline_revisions_guard()")
        op.execute("DROP FUNCTION immutable_timeline_revisions_insert_guard()")
