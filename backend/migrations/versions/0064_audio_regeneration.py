"""Bind single-line regeneration to an immutable source audio version."""

import sqlalchemy as sa
from alembic import context, op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade():
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        # Native ADD COLUMN preserves the existing immutable-history triggers.
        op.execute(
            "ALTER TABLE audio_tts_requests ADD COLUMN source_audio_version_id "
            "CHAR(32) REFERENCES asset_versions (id)"
        )
        origin_value = (
            "json_extract(g.snapshot_json, '$.context.audio_line.source_audio_version_id')"
        )
        origin_matches = f"replace({origin_value}, '-', '')=NEW.source_audio_version_id"
    else:
        op.add_column(
            "audio_tts_requests",
            sa.Column(
                "source_audio_version_id",
                sa.Uuid(),
                sa.ForeignKey("asset_versions.id"),
                nullable=True,
            ),
        )
        origin_value = "g.snapshot_json->'context'->'audio_line'->>'source_audio_version_id'"
        origin_matches = f"{origin_value}=NEW.source_audio_version_id::text"
    valid_source = (
        "EXISTS (SELECT 1 FROM audio_line_bindings b "
        "JOIN asset_versions v ON v.id=b.asset_version_id "
        "JOIN visual_assets a ON a.id=v.asset_id "
        "WHERE b.asset_version_id=NEW.source_audio_version_id "
        "AND b.snapshot_id=NEW.snapshot_id AND b.line_id=NEW.line_id "
        "AND v.asset_id=NEW.target_asset_id AND a.project_id=NEW.project_id "
        "AND a.asset_type='AUDIO')"
    )
    valid_origin = (
        "EXISTS (SELECT 1 FROM generation_jobs j "
        "JOIN dependency_manifests g ON g.id=j.input_manifest_id "
        "WHERE j.id=NEW.job_id AND "
        f"((NEW.source_audio_version_id IS NULL AND {origin_value} IS NULL) "
        f"OR ({origin_matches})))"
    )
    invalid = (
        f"(NEW.source_audio_version_id IS NOT NULL AND NOT ({valid_source})) "
        f"OR NOT ({valid_origin})"
    )
    if dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER immutable_audio_tts_regeneration_insert BEFORE INSERT "
            f"ON audio_tts_requests WHEN {invalid} BEGIN SELECT RAISE(ABORT, "
            "'IMMUTABLE_HISTORY: audio regeneration source'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION immutable_audio_tts_regeneration_insert_guard() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF {invalid} THEN RAISE EXCEPTION "
            "'IMMUTABLE_HISTORY: audio regeneration source'; END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_audio_tts_regeneration_insert BEFORE INSERT "
            "ON audio_tts_requests FOR EACH ROW EXECUTE FUNCTION "
            "immutable_audio_tts_regeneration_insert_guard()"
        )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Audio regeneration downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE audio_tts_requests IN ACCESS EXCLUSIVE MODE")
    if db.scalar(
        sa.text(
            "SELECT 1 FROM audio_tts_requests WHERE source_audio_version_id IS NOT NULL LIMIT 1"
        )
    ):
        raise RuntimeError("Cannot discard audio regeneration history")
    if db.dialect.name == "sqlite":
        op.execute("DROP TRIGGER immutable_audio_tts_regeneration_insert")
    else:
        op.execute("DROP TRIGGER immutable_audio_tts_regeneration_insert ON audio_tts_requests")
        op.execute("DROP FUNCTION immutable_audio_tts_regeneration_insert_guard()")
    # Both supported databases can remove the nullable column without rebuilding history.
    op.drop_column("audio_tts_requests", "source_audio_version_id")
