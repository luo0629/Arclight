"""Exact timeline versions and immutable track/clip snapshots."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TABLES = ("timelines", "timeline_tracks", "timeline_clips")
TIMELINE_FIELDS = (
    "id",
    "episode_id",
    "storyboard_id",
    "version_no",
    "default_duration_ms",
    "duration_ms",
    "output_profile_json",
    "creation_key",
    "request_sha256",
    "created_by",
    "created_at",
)


def identity(reference=None):
    arguments = [sa.ForeignKey(reference)] if reference else []
    return (
        sa.Column("id", sa.Uuid(), *arguments, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def guards():
    sqlite = op.get_bind().dialect.name == "sqlite"
    complete = (
        "(SELECT count(*) FROM timeline_tracks WHERE timeline_id=NEW.id)=5 AND "
        "(SELECT count(*) FROM timeline_clips WHERE timeline_id=NEW.id)>0 AND "
        "(SELECT sum(c.duration_ms) FROM timeline_clips c "
        "JOIN timeline_tracks t ON t.id=c.track_id "
        "WHERE c.timeline_id=NEW.id AND t.kind='VIDEO')=NEW.duration_ms AND "
        "(SELECT max(c.start_ms+c.duration_ms) FROM timeline_clips c JOIN timeline_tracks t "
        "ON t.id=c.track_id WHERE c.timeline_id=NEW.id AND t.kind='VIDEO')=NEW.duration_ms"
    )
    unchanged = " AND ".join(
        f"NEW.{field} IS OLD.{field}" if sqlite else f"NEW.{field} IS NOT DISTINCT FROM OLD.{field}"
        for field in TIMELINE_FIELDS
    )
    seal = f"NOT OLD.sealed AND NEW.sealed AND {unchanged} AND ({complete})"
    parent_scope = (
        "EXISTS (SELECT 1 FROM episodes e JOIN storyboards b ON b.episode_id=e.id "
        "JOIN asset_versions v ON v.id=NEW.id JOIN visual_assets a ON a.id=v.asset_id "
        "WHERE e.id=NEW.episode_id AND b.id=NEW.storyboard_id AND a.project_id=e.project_id "
        "AND a.asset_type='TIMELINE' AND v.version_no=NEW.version_no)"
    )
    insert_checks = {
        "timelines": (
            "NEW.sealed OR NOT (" + parent_scope + ") OR EXISTS (SELECT 1 FROM timelines "
            "WHERE id=NEW.id OR (episode_id=NEW.episode_id AND "
            "(version_no=NEW.version_no OR creation_key=NEW.creation_key)))"
        ),
        "timeline_tracks": (
            "NOT EXISTS (SELECT 1 FROM timelines WHERE id=NEW.timeline_id AND NOT sealed) "
            "OR EXISTS (SELECT 1 FROM timeline_tracks WHERE id=NEW.id OR "
            "(timeline_id=NEW.timeline_id AND (kind=NEW.kind OR ordinal=NEW.ordinal)))"
        ),
        "timeline_clips": (
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
            "OR (t.kind IN ('MUSIC','SFX') AND a.asset_type='AUDIO') "
            "OR (t.kind='SUBTITLE' AND a.asset_type='SUBTITLE' AND NEW.line_id IS NOT NULL)) "
            "AND (NEW.audio_version_id IS NULL OR (t.kind='SUBTITLE' AND EXISTS "
            "(SELECT 1 FROM asset_versions av JOIN visual_assets aa ON aa.id=av.asset_id "
            "WHERE av.id=NEW.audio_version_id AND aa.project_id=e.project_id "
            "AND aa.asset_type='AUDIO')))) "
            "OR EXISTS (SELECT 1 FROM timeline_clips WHERE id=NEW.id OR "
            "(track_id=NEW.track_id AND ordinal=NEW.ordinal))"
        ),
    }
    for table in TABLES:
        failure = "RAISE EXCEPTION 'IMMUTABLE_HISTORY: timeline';"
        if sqlite:
            for operation in ("UPDATE", "DELETE"):
                condition = (
                    f" WHEN NOT ({seal})" if table == "timelines" and operation == "UPDATE" else ""
                )
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} "
                    f"ON {table}{condition} "
                    "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: timeline'); END"
                )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"WHEN {insert_checks[table]} "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: timeline scope'); END"
            )
        else:
            allowance = (
                f"IF TG_OP='UPDATE' AND ({seal}) THEN RETURN NEW; END IF; "
                if table == "timelines"
                else ""
            )
            op.execute(
                f"CREATE FUNCTION immutable_{table}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN {allowance}{failure} END; $$"
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
                f"AS $$ BEGIN IF {insert_checks[table]} THEN {failure} END IF; RETURN NEW; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION immutable_{table}_insert_guard()"
            )


def upgrade():
    op.create_table(
        "timelines",
        *identity("asset_versions.id"),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id"), nullable=False),
        sa.Column("storyboard_id", sa.Uuid(), sa.ForeignKey("storyboards.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("default_duration_ms", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("output_profile_json", JSON, nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("sealed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("episode_id", "version_no", name="uq_timeline_episode_version"),
        sa.UniqueConstraint("episode_id", "creation_key", name="uq_timeline_episode_creation"),
        sa.CheckConstraint("version_no > 0", name="timeline_version_positive"),
        sa.CheckConstraint(
            "default_duration_ms >= 500 AND default_duration_ms <= 30000",
            name="timeline_default_duration",
        ),
        sa.CheckConstraint("duration_ms > 0 AND duration_ms <= 3600000", name="timeline_duration"),
    )
    op.create_index("ix_timelines_episode_id", "timelines", ["episode_id"])
    op.create_table(
        "timeline_tracks",
        *identity(),
        sa.Column("timeline_id", sa.Uuid(), sa.ForeignKey("timelines.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint("timeline_id", "id", name="uq_timeline_track_identity"),
        sa.UniqueConstraint("timeline_id", "kind", name="uq_timeline_track_kind"),
        sa.UniqueConstraint("timeline_id", "ordinal", name="uq_timeline_track_ordinal"),
        sa.CheckConstraint(
            "kind IN ('VIDEO','VOICE','SFX','MUSIC','SUBTITLE')", name="timeline_track_kind"
        ),
        sa.CheckConstraint("ordinal >= 0 AND ordinal < 5", name="timeline_track_ordinal"),
    )
    op.create_index("ix_timeline_tracks_timeline_id", "timeline_tracks", ["timeline_id"])
    op.create_table(
        "timeline_clips",
        *identity(),
        sa.Column("timeline_id", sa.Uuid(), sa.ForeignKey("timelines.id"), nullable=False),
        sa.Column("track_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "source_asset_version_id", sa.Uuid(), sa.ForeignKey("asset_versions.id"), nullable=False
        ),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("source_start_ms", sa.Integer(), nullable=False),
        sa.Column("shot_id", sa.Uuid(), sa.ForeignKey("shots.id")),
        sa.Column("script_version_id", sa.Uuid(), sa.ForeignKey("scripts.id")),
        sa.Column("line_id", sa.String(100)),
        sa.Column("audio_version_id", sa.Uuid(), sa.ForeignKey("asset_versions.id")),
        sa.Column("review_id", sa.Uuid()),
        sa.Column("review_storyboard_id", sa.Uuid(), sa.ForeignKey("storyboards.id")),
        sa.Column("review_shot_id", sa.Uuid(), sa.ForeignKey("shots.id")),
        sa.ForeignKeyConstraint(
            ["timeline_id", "track_id"], ["timeline_tracks.timeline_id", "timeline_tracks.id"]
        ),
        sa.UniqueConstraint("track_id", "ordinal"),
        sa.CheckConstraint("ordinal >= 0 AND ordinal < 256", name="timeline_clip_ordinal"),
        sa.CheckConstraint(
            "start_ms >= 0 AND duration_ms > 0 AND start_ms + duration_ms <= 3600000 "
            "AND source_start_ms >= 0 AND source_start_ms <= 3600000",
            name="timeline_clip_time",
        ),
        sa.CheckConstraint(
            "(script_version_id IS NULL) = (line_id IS NULL)", name="timeline_clip_line"
        ),
    )
    op.create_index("ix_timeline_clips_timeline_id", "timeline_clips", ["timeline_id"])
    guards()


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Timeline downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE timelines, timeline_tracks, timeline_clips IN ACCESS EXCLUSIVE MODE")
    if any(db.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        raise RuntimeError("Cannot discard timeline history")
    for table in reversed(TABLES):
        op.drop_table(table)
        if db.dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION immutable_{table}_guard()")
            op.execute(f"DROP FUNCTION immutable_{table}_insert_guard()")
