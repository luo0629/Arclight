"""Freeze approved dialogue extraction and exact audio version bindings."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None
TABLES = ("audio_snapshots", "audio_line_bindings")


def identity():
    return (
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade():
    op.create_table(
        "audio_snapshots",
        *identity(),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id"), nullable=False),
        sa.Column("script_version_id", sa.Uuid(), sa.ForeignKey("scripts.id"), nullable=False),
        sa.Column("script_sha256", sa.String(64), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column(
            "lines_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("episode_id", "creation_key"),
        sa.CheckConstraint("line_count >= 0", name="audio_line_count"),
    )
    op.create_index("ix_audio_snapshots_episode_id", "audio_snapshots", ["episode_id"])
    op.create_table(
        "audio_line_bindings",
        *identity(),
        sa.Column("snapshot_id", sa.Uuid(), sa.ForeignKey("audio_snapshots.id"), nullable=False),
        sa.Column("line_id", sa.String(100), nullable=False),
        sa.Column(
            "asset_version_id", sa.Uuid(), sa.ForeignKey("asset_versions.id"), nullable=False
        ),
        sa.Column(
            "dependency_manifest_id",
            sa.Uuid(),
            sa.ForeignKey("dependency_manifests.id"),
            nullable=False,
        ),
        sa.Column("file_id", sa.Uuid(), sa.ForeignKey("stored_files.id"), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("asset_version_id"),
        sa.CheckConstraint("duration_ms > 0", name="audio_duration"),
    )
    op.create_index("ix_audio_line_bindings_snapshot_id", "audio_line_bindings", ["snapshot_id"])
    checks = {
        "audio_snapshots": (
            "EXISTS (SELECT 1 FROM audio_snapshots WHERE id=NEW.id OR "
            "(episode_id=NEW.episode_id AND creation_key=NEW.creation_key)) OR NOT EXISTS "
            "(SELECT 1 FROM scripts s JOIN asset_versions v ON v.id=s.id "
            "WHERE s.id=NEW.script_version_id AND s.episode_id=NEW.episode_id "
            "AND s.status='APPROVED' AND v.status IN ('APPROVED','LOCKED') "
            "AND NOT v.stale AND v.checksum=NEW.script_sha256)"
        ),
        "audio_line_bindings": (
            "EXISTS (SELECT 1 FROM audio_line_bindings WHERE id=NEW.id OR "
            "asset_version_id=NEW.asset_version_id) OR NOT EXISTS "
            "(SELECT 1 FROM audio_snapshots s JOIN episodes e ON e.id=s.episode_id "
            "JOIN asset_versions v ON v.id=NEW.asset_version_id "
            "JOIN visual_assets a ON a.id=v.asset_id "
            "JOIN stored_files f ON f.id=NEW.file_id "
            "JOIN dependency_outputs o ON o.asset_version_id=v.id "
            "JOIN dependency_manifests m ON m.id=o.dependency_manifest_id "
            "WHERE s.id=NEW.snapshot_id AND a.project_id=e.project_id AND a.asset_type='AUDIO' "
            "AND f.asset_version_id=v.id AND f.project_id=e.project_id "
            "AND f.checksum=v.checksum AND m.id=NEW.dependency_manifest_id "
            "AND m.project_id=e.project_id AND m.target_asset_id=a.id)"
        ),
    }
    if op.get_bind().dialect.name == "sqlite":
        line_exists = (
            "EXISTS (SELECT 1 FROM audio_snapshots s, json_each(s.lines_json) line "
            "WHERE s.id=NEW.snapshot_id AND json_extract(line.value, '$.line_id')=NEW.line_id)"
        )
        snapshot_shape = (
            "json_type(NEW.lines_json)='array' AND json_array_length(NEW.lines_json)=NEW.line_count"
        )
    else:
        line_exists = (
            "EXISTS (SELECT 1 FROM audio_snapshots s, jsonb_array_elements(s.lines_json) line "
            "WHERE s.id=NEW.snapshot_id AND line->>'line_id'=NEW.line_id)"
        )
        snapshot_shape = (
            "jsonb_typeof(NEW.lines_json)='array' AND "
            "jsonb_array_length(NEW.lines_json)=NEW.line_count"
        )
    checks["audio_line_bindings"] += f" OR NOT ({line_exists})"
    checks["audio_snapshots"] += f" OR NOT ({snapshot_shape})"
    for table in TABLES:
        if op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE", "INSERT"):
                condition = f" WHEN {checks[table]}" if action == "INSERT" else ""
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} "
                    f"ON {table}{condition} BEGIN SELECT RAISE(ABORT, "
                    "'IMMUTABLE_HISTORY: audio production'); END"
                )
        else:
            op.execute(
                f"CREATE FUNCTION immutable_{table}_guard() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: audio production'; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION immutable_{table}_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION immutable_{table}_guard()"
            )
            op.execute(
                f"CREATE FUNCTION immutable_{table}_insert_guard() "
                "RETURNS trigger LANGUAGE plpgsql "
                f"AS $$ BEGIN IF {checks[table]} THEN RAISE EXCEPTION "
                "'IMMUTABLE_HISTORY: audio scope'; END IF; RETURN NEW; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION immutable_{table}_insert_guard()"
            )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Audio downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE audio_snapshots, audio_line_bindings IN ACCESS EXCLUSIVE MODE")
    if any(db.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        raise RuntimeError("Cannot discard audio production history")
    for table in reversed(TABLES):
        op.drop_table(table)
        if db.dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION immutable_{table}_guard()")
            op.execute(f"DROP FUNCTION immutable_{table}_insert_guard()")
