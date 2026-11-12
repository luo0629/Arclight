"""Freeze character voice configurations and exact dialogue generation requests."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None
TABLES = ("audio_voice_versions", "audio_tts_requests")


def identity():
    return (
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade():
    op.create_table(
        "audio_voice_versions",
        *identity(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("speaker", sa.String(100), nullable=False),
        sa.Column("character_asset_id", sa.Uuid(), sa.ForeignKey("visual_assets.id")),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "configuration_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "service_snapshot_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint(
            "project_id", "speaker", "version_no", name="uq_audio_voice_speaker_version"
        ),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_audio_voice_creation"),
        sa.CheckConstraint("version_no > 0", name="audio_voice_number"),
    )
    op.create_index("ix_audio_voice_versions_project_id", "audio_voice_versions", ["project_id"])
    op.create_table(
        "audio_tts_requests",
        *identity(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), sa.ForeignKey("audio_snapshots.id"), nullable=False),
        sa.Column("line_id", sa.String(100), nullable=False),
        sa.Column(
            "voice_version_id", sa.Uuid(), sa.ForeignKey("audio_voice_versions.id"), nullable=False
        ),
        sa.Column("target_asset_id", sa.Uuid(), sa.ForeignKey("visual_assets.id"), nullable=False),
        sa.Column(
            "dependency_manifest_id",
            sa.Uuid(),
            sa.ForeignKey("dependency_manifests.id"),
            nullable=False,
        ),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_audio_tts_creation"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_audio_tts_requests_project_id", "audio_tts_requests", ["project_id"])
    op.create_index("ix_audio_tts_requests_snapshot_id", "audio_tts_requests", ["snapshot_id"])
    checks = {
        "audio_voice_versions": (
            "EXISTS (SELECT 1 FROM audio_voice_versions WHERE id=NEW.id OR "
            "(project_id=NEW.project_id AND speaker=NEW.speaker AND version_no=NEW.version_no) OR "
            "(project_id=NEW.project_id AND creation_key=NEW.creation_key)) OR "
            "(NEW.character_asset_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM visual_assets a WHERE a.id=NEW.character_asset_id "
            "AND a.project_id=NEW.project_id AND a.asset_type='CHARACTER'))"
        ),
        "audio_tts_requests": (
            "EXISTS (SELECT 1 FROM audio_tts_requests WHERE id=NEW.id OR job_id=NEW.job_id OR "
            "(project_id=NEW.project_id AND creation_key=NEW.creation_key)) OR NOT EXISTS "
            "(SELECT 1 FROM audio_snapshots s JOIN episodes e ON e.id=s.episode_id "
            "JOIN audio_voice_versions v ON v.id=NEW.voice_version_id "
            "JOIN visual_assets a ON a.id=NEW.target_asset_id "
            "JOIN dependency_manifests m ON m.id=NEW.dependency_manifest_id "
            "JOIN generation_jobs j ON j.id=NEW.job_id "
            "JOIN dependency_manifests g ON g.id=j.input_manifest_id "
            "WHERE s.id=NEW.snapshot_id AND e.project_id=NEW.project_id "
            "AND v.project_id=NEW.project_id AND a.project_id=NEW.project_id "
            "AND a.asset_type='AUDIO' AND m.project_id=NEW.project_id "
            "AND m.target_asset_id=a.id AND j.project_id=NEW.project_id "
            "AND j.task_type='AUDIO' AND j.target_type='AUDIO' AND j.target_asset_id=a.id "
            "AND j.episode_id=s.episode_id AND g.project_id=NEW.project_id "
            "AND g.target_asset_id=a.id)"
        ),
    }
    if op.get_bind().dialect.name == "sqlite":
        line_exists = (
            "EXISTS (SELECT 1 FROM audio_snapshots s "
            "JOIN audio_voice_versions v ON v.id=NEW.voice_version_id, "
            "json_each(s.lines_json) line WHERE s.id=NEW.snapshot_id "
            "AND json_extract(line.value, '$.line_id')=NEW.line_id "
            "AND json_extract(line.value, '$.speaker')=v.speaker)"
        )
    else:
        line_exists = (
            "EXISTS (SELECT 1 FROM audio_snapshots s "
            "JOIN audio_voice_versions v ON v.id=NEW.voice_version_id, "
            "jsonb_array_elements(s.lines_json) line WHERE s.id=NEW.snapshot_id "
            "AND line->>'line_id'=NEW.line_id AND line->>'speaker'=v.speaker)"
        )
    if op.get_bind().dialect.name == "sqlite":
        origin_matches = (
            "EXISTS (SELECT 1 FROM generation_jobs j "
            "JOIN dependency_manifests g ON g.id=j.input_manifest_id "
            "WHERE j.id=NEW.job_id AND replace(json_extract(g.snapshot_json, "
            "'$.context.audio_line.dependency_manifest_id'), '-', '')=NEW.dependency_manifest_id)"
        )
    else:
        origin_matches = (
            "EXISTS (SELECT 1 FROM generation_jobs j "
            "JOIN dependency_manifests g ON g.id=j.input_manifest_id "
            "WHERE j.id=NEW.job_id AND g.snapshot_json->'context'->'audio_line'"
            "->>'dependency_manifest_id'=NEW.dependency_manifest_id::text)"
        )
    checks["audio_tts_requests"] += f" OR NOT ({line_exists}) OR NOT ({origin_matches})"
    for table in TABLES:
        if op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE", "INSERT"):
                condition = f" WHEN {checks[table]}" if action == "INSERT" else ""
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} "
                    f"ON {table}{condition} BEGIN SELECT RAISE(ABORT, "
                    "'IMMUTABLE_HISTORY: audio TTS'); END"
                )
        else:
            op.execute(
                f"CREATE FUNCTION immutable_{table}_guard() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: audio TTS'; END; $$"
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
                "'IMMUTABLE_HISTORY: audio TTS scope'; END IF; RETURN NEW; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION immutable_{table}_insert_guard()"
            )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Audio TTS downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE audio_voice_versions, audio_tts_requests IN ACCESS EXCLUSIVE MODE")
    if any(db.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        raise RuntimeError("Cannot discard audio TTS history")
    for table in reversed(TABLES):
        op.drop_table(table)
        if db.dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION immutable_{table}_guard()")
            op.execute(f"DROP FUNCTION immutable_{table}_insert_guard()")
