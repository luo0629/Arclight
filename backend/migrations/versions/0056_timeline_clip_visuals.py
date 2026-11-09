"""Add immutable visual edits without rewriting legacy timeline snapshots."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "timeline_clip_visuals",
        sa.Column("id", sa.Uuid(), sa.ForeignKey("timeline_clips.id"), primary_key=True),
        sa.Column("timeline_id", sa.Uuid(), sa.ForeignKey("timelines.id"), nullable=False),
        sa.Column("settings_json", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_timeline_clip_visuals_timeline_id", "timeline_clip_visuals", ["timeline_id"]
    )
    sqlite = op.get_bind().dialect.name == "sqlite"
    schema = (
        "coalesce(json_extract(v.metadata_json, '$.timeline_schema_version'), 1)"
        if sqlite
        else "coalesce((v.metadata_json->>'timeline_schema_version')::integer, 1)"
    )
    invalid = (
        "NOT EXISTS (SELECT 1 FROM timeline_clips c JOIN timelines l ON l.id=c.timeline_id "
        "JOIN timeline_tracks t ON t.id=c.track_id JOIN asset_versions v ON v.id=l.id "
        "WHERE c.id=NEW.id AND l.id=NEW.timeline_id AND t.kind='VIDEO' AND NOT l.sealed "
        f"AND {schema}=2) "
        "OR EXISTS (SELECT 1 FROM timeline_clip_visuals WHERE id=NEW.id)"
    )
    incomplete = (
        "NOT OLD.sealed AND NEW.sealed AND EXISTS (SELECT 1 FROM asset_versions v "
        f"WHERE v.id=NEW.id AND (({schema}=2 AND "
        "(SELECT count(*) FROM timeline_clip_visuals WHERE timeline_id=NEW.id) "
        "<> (SELECT count(*) FROM timeline_clips c JOIN timeline_tracks t ON t.id=c.track_id "
        "WHERE c.timeline_id=NEW.id AND t.kind='VIDEO')) "
        f"OR ({schema}=1 AND EXISTS "
        "(SELECT 1 FROM timeline_clip_visuals WHERE timeline_id=NEW.id)) "
        f"OR {schema} NOT IN (1,2)))"
    )
    if sqlite:
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_timeline_clip_visuals_{operation.lower()} "
                f"BEFORE {operation} ON timeline_clip_visuals "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: timeline visual'); END"
            )
        op.execute(
            "CREATE TRIGGER immutable_timeline_clip_visuals_insert "
            f"BEFORE INSERT ON timeline_clip_visuals WHEN {invalid} "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: timeline visual scope'); END"
        )
        op.execute(
            "CREATE TRIGGER timeline_clip_visuals_seal BEFORE UPDATE ON timelines "
            f"WHEN {incomplete} "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: incomplete timeline visuals'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION immutable_timeline_clip_visuals_guard() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: timeline visual'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_timeline_clip_visuals_write BEFORE UPDATE OR DELETE "
            "ON timeline_clip_visuals FOR EACH ROW "
            "EXECUTE FUNCTION immutable_timeline_clip_visuals_guard()"
        )
        op.execute(
            "CREATE TRIGGER immutable_timeline_clip_visuals_truncate BEFORE TRUNCATE "
            "ON timeline_clip_visuals FOR EACH STATEMENT "
            "EXECUTE FUNCTION immutable_timeline_clip_visuals_guard()"
        )
        op.execute(
            "CREATE FUNCTION immutable_timeline_clip_visuals_insert_guard() RETURNS trigger "
            f"LANGUAGE plpgsql AS $$ BEGIN IF {invalid} THEN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: timeline visual scope'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_timeline_clip_visuals_insert BEFORE INSERT "
            "ON timeline_clip_visuals FOR EACH ROW "
            "EXECUTE FUNCTION immutable_timeline_clip_visuals_insert_guard()"
        )
        op.execute(
            "CREATE FUNCTION timeline_clip_visuals_seal_guard() RETURNS trigger "
            f"LANGUAGE plpgsql AS $$ BEGIN IF {incomplete} THEN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: incomplete timeline visuals'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER timeline_clip_visuals_seal BEFORE UPDATE ON timelines "
            "FOR EACH ROW EXECUTE FUNCTION timeline_clip_visuals_seal_guard()"
        )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Timeline visual downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE timeline_clip_visuals IN ACCESS EXCLUSIVE MODE")
    if db.scalar(sa.text("SELECT 1 FROM timeline_clip_visuals LIMIT 1")):
        raise RuntimeError("Cannot discard timeline visual history")
    op.execute(
        "DROP TRIGGER timeline_clip_visuals_seal"
        + (" ON timelines" if db.dialect.name == "postgresql" else "")
    )
    op.drop_table("timeline_clip_visuals")
    if db.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_timeline_clip_visuals_guard()")
        op.execute("DROP FUNCTION immutable_timeline_clip_visuals_insert_guard()")
        op.execute("DROP FUNCTION timeline_clip_visuals_seal_guard()")
