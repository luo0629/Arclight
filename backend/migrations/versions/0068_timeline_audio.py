"""Freeze clip audio automation and admit exact registered music resources."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def clip_guard(sqlite: bool, *, music: bool) -> str:
    role = (
        "json_extract(v.metadata_json, '$.audio_resource.purpose_role')"
        if sqlite
        else "v.metadata_json->'audio_resource'->>'purpose_role'"
    )
    sounds = "(t.kind IN ('MUSIC','SFX') AND a.asset_type='AUDIO')"
    if music:
        sounds += (
            " OR (a.asset_type='MUSIC' AND "
            f"((t.kind='MUSIC' AND {role}='BACKGROUND') OR "
            f"(t.kind='SFX' AND {role} IN ('AMBIENCE','TRANSITION'))))"
        )
    return (
        "NOT EXISTS (SELECT 1 FROM timelines l JOIN timeline_tracks t ON t.timeline_id=l.id "
        "JOIN episodes e ON e.id=l.episode_id JOIN storyboards b ON b.id=l.storyboard_id "
        "JOIN asset_versions v ON v.id=NEW.source_asset_version_id "
        "JOIN visual_assets a ON a.id=v.asset_id "
        "WHERE l.id=NEW.timeline_id AND t.id=NEW.track_id AND NOT l.sealed "
        "AND a.project_id=e.project_id AND v.checksum=NEW.source_sha256 "
        "AND NEW.start_ms+NEW.duration_ms<=l.duration_ms "
        "AND (NEW.script_version_id IS NULL OR NEW.script_version_id=b.script_version_id) "
        "AND ((t.kind='VIDEO' AND a.asset_type IN ('IMAGE','SHOT_IMAGE') "
        "AND EXISTS (SELECT 1 FROM shots s WHERE s.id=NEW.shot_id AND s.storyboard_id=b.id)) "
        "OR (t.kind='VOICE' AND a.asset_type='AUDIO' AND NEW.line_id IS NOT NULL) "
        f"OR ({sounds}) "
        "OR (t.kind='SUBTITLE' AND a.asset_type='SUBTITLE' AND NEW.line_id IS NOT NULL)) "
        "AND (NEW.audio_version_id IS NULL OR (t.kind='SUBTITLE' AND EXISTS "
        "(SELECT 1 FROM asset_versions av JOIN visual_assets aa ON aa.id=av.asset_id "
        "WHERE av.id=NEW.audio_version_id AND aa.project_id=e.project_id "
        "AND aa.asset_type='AUDIO')))) "
        "OR EXISTS (SELECT 1 FROM timeline_clips WHERE id=NEW.id OR "
        "(track_id=NEW.track_id AND ordinal=NEW.ordinal))"
    )


def install_clip_guard(*, music: bool) -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    invalid = clip_guard(sqlite, music=music)
    if sqlite:
        op.execute("DROP TRIGGER immutable_timeline_clips_insert")
        op.execute(
            "CREATE TRIGGER immutable_timeline_clips_insert BEFORE INSERT ON timeline_clips "
            f"WHEN {invalid} "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: timeline scope'); END"
        )
    else:
        op.execute(
            "CREATE OR REPLACE FUNCTION immutable_timeline_clips_insert_guard() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF {invalid} THEN RAISE EXCEPTION 'IMMUTABLE_HISTORY: timeline'; "
            "END IF; RETURN NEW; END; $$"
        )


def upgrade():
    op.create_table(
        "timeline_clip_audio",
        sa.Column("id", sa.Uuid(), sa.ForeignKey("timeline_clips.id"), primary_key=True),
        sa.Column("timeline_id", sa.Uuid(), sa.ForeignKey("timelines.id"), nullable=False),
        sa.Column("settings_json", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_timeline_clip_audio_timeline_id", "timeline_clip_audio", ["timeline_id"])
    sqlite = op.get_bind().dialect.name == "sqlite"
    role = (
        "json_extract(NEW.settings_json, '$.purpose_role')"
        if sqlite
        else "NEW.settings_json->>'purpose_role'"
    )
    source_role = (
        "json_extract(v.metadata_json, '$.audio_resource.purpose_role')"
        if sqlite
        else "v.metadata_json->'audio_resource'->>'purpose_role'"
    )
    expected = (
        "coalesce(json_extract(v.metadata_json, '$.timeline_audio_clip_count'), 0)"
        if sqlite
        else "coalesce((v.metadata_json->>'timeline_audio_clip_count')::integer, 0)"
    )
    parent_expected = expected.replace("v.metadata_json", "tv.metadata_json")
    invalid = (
        "NOT EXISTS (SELECT 1 FROM timeline_clips c JOIN timelines l ON l.id=c.timeline_id "
        "JOIN timeline_tracks t ON t.id=c.track_id "
        "JOIN asset_versions tv ON tv.id=l.id "
        "JOIN asset_versions v ON v.id=c.source_asset_version_id "
        "JOIN visual_assets a ON a.id=v.asset_id "
        "WHERE c.id=NEW.id AND l.id=NEW.timeline_id AND t.timeline_id=l.id AND NOT l.sealed "
        f"AND {parent_expected}>0 "
        f"AND ((t.kind='MUSIC' AND {role}='BACKGROUND') OR "
        f"(t.kind='SFX' AND {role} IN ('AMBIENCE','TRANSITION'))) "
        f"AND (a.asset_type='AUDIO' OR (a.asset_type='MUSIC' AND {source_role}={role}))) "
        "OR EXISTS (SELECT 1 FROM timeline_clip_audio WHERE id=NEW.id)"
    )
    incomplete = (
        "NOT OLD.sealed AND NEW.sealed AND EXISTS (SELECT 1 FROM asset_versions v "
        f"WHERE v.id=NEW.id AND ({expected}<0 OR {expected}>1280 OR "
        f"{expected}<>(SELECT count(*) FROM timeline_clip_audio WHERE timeline_id=NEW.id) "
        "OR EXISTS (SELECT 1 FROM timeline_clips c "
        "JOIN asset_versions av ON av.id=c.source_asset_version_id "
        "JOIN visual_assets a ON a.id=av.asset_id "
        "WHERE c.timeline_id=NEW.id AND a.asset_type='MUSIC' "
        "AND NOT EXISTS (SELECT 1 FROM timeline_clip_audio x WHERE x.id=c.id))))"
    )
    if sqlite:
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_timeline_clip_audio_{operation.lower()} "
                f"BEFORE {operation} ON timeline_clip_audio "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: timeline audio'); END"
            )
        op.execute(
            "CREATE TRIGGER immutable_timeline_clip_audio_insert "
            f"BEFORE INSERT ON timeline_clip_audio WHEN {invalid} "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: timeline audio scope'); END"
        )
        op.execute(
            "CREATE TRIGGER timeline_clip_audio_seal BEFORE UPDATE ON timelines "
            f"WHEN {incomplete} "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: incomplete timeline audio'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION immutable_timeline_clip_audio_guard() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: timeline audio'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_timeline_clip_audio_write BEFORE UPDATE OR DELETE "
            "ON timeline_clip_audio FOR EACH ROW "
            "EXECUTE FUNCTION immutable_timeline_clip_audio_guard()"
        )
        op.execute(
            "CREATE TRIGGER immutable_timeline_clip_audio_truncate BEFORE TRUNCATE "
            "ON timeline_clip_audio FOR EACH STATEMENT "
            "EXECUTE FUNCTION immutable_timeline_clip_audio_guard()"
        )
        op.execute(
            "CREATE FUNCTION immutable_timeline_clip_audio_insert_guard() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "PERFORM 1 FROM timelines WHERE id=NEW.timeline_id FOR UPDATE; "
            f"IF {invalid} THEN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: timeline audio scope'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_timeline_clip_audio_insert BEFORE INSERT "
            "ON timeline_clip_audio FOR EACH ROW "
            "EXECUTE FUNCTION immutable_timeline_clip_audio_insert_guard()"
        )
        op.execute(
            "CREATE FUNCTION timeline_clip_audio_seal_guard() RETURNS trigger "
            f"LANGUAGE plpgsql AS $$ BEGIN IF {incomplete} THEN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: incomplete timeline audio'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER timeline_clip_audio_seal BEFORE UPDATE ON timelines "
            "FOR EACH ROW EXECUTE FUNCTION timeline_clip_audio_seal_guard()"
        )
    install_clip_guard(music=True)


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Timeline audio downgrade requires an online history check")
    db = op.get_bind()
    sqlite = db.dialect.name == "sqlite"
    if sqlite:
        driver = db.connection.driver_connection
        if not driver.in_transaction:
            op.execute("BEGIN IMMEDIATE")
    else:
        op.execute("LOCK TABLE timeline_clip_audio IN ACCESS EXCLUSIVE MODE")
    if db.scalar(sa.text("SELECT 1 FROM timeline_clip_audio LIMIT 1")):
        raise RuntimeError("Cannot discard timeline audio history")
    install_clip_guard(music=False)
    op.execute("DROP TRIGGER timeline_clip_audio_seal" + ("" if sqlite else " ON timelines"))
    op.drop_table("timeline_clip_audio")
    if not sqlite:
        op.execute("DROP FUNCTION immutable_timeline_clip_audio_guard()")
        op.execute("DROP FUNCTION immutable_timeline_clip_audio_insert_guard()")
        op.execute("DROP FUNCTION timeline_clip_audio_seal_guard()")
