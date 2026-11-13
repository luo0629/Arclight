"""Freeze exact subtitle production documents and SRT receipts."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "subtitle_productions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id"), nullable=False),
        sa.Column("timeline_id", sa.Uuid(), sa.ForeignKey("timelines.id"), nullable=False),
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
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "snapshot_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("episode_id", "creation_key"),
        sa.UniqueConstraint("asset_version_id"),
        sa.UniqueConstraint("file_id"),
    )
    op.create_index("ix_subtitle_productions_episode_id", "subtitle_productions", ["episode_id"])
    scope = (
        "EXISTS (SELECT 1 FROM subtitle_productions p WHERE p.id=NEW.id "
        "OR p.asset_version_id=NEW.asset_version_id OR p.file_id=NEW.file_id "
        "OR (p.episode_id=NEW.episode_id AND p.creation_key=NEW.creation_key)) "
        "OR NOT EXISTS (SELECT 1 FROM episodes e "
        "JOIN timelines t ON t.id=NEW.timeline_id AND t.episode_id=e.id AND t.sealed "
        "JOIN asset_versions v ON v.id=NEW.asset_version_id "
        "JOIN visual_assets a ON a.id=v.asset_id AND a.project_id=e.project_id "
        "JOIN stored_files f ON f.id=NEW.file_id AND f.asset_version_id=v.id "
        "JOIN dependency_outputs o ON o.asset_version_id=v.id "
        "JOIN dependency_manifests m ON m.id=o.dependency_manifest_id "
        "WHERE e.id=NEW.episode_id AND a.asset_type='SUBTITLE' "
        "AND f.project_id=e.project_id AND f.checksum=v.checksum "
        "AND f.object_key=v.object_key AND f.media_type='text/plain' AND f.rights_confirmed "
        "AND m.id=NEW.dependency_manifest_id AND m.project_id=e.project_id "
        "AND m.target_asset_id=a.id AND v.creation_key=NEW.creation_key)"
    )
    if op.get_bind().dialect.name == "sqlite":
        shape = (
            "json_type(NEW.snapshot_json, '$.cues')='array' "
            "AND json_array_length(NEW.snapshot_json, '$.cues') BETWEEN 1 AND 256 "
            "AND json_extract(NEW.snapshot_json, '$.schema_version')='subtitle-track/1' "
            "AND replace(json_extract(NEW.snapshot_json, '$.episode_id'),'-','')=NEW.episode_id "
            "AND replace(json_extract(NEW.snapshot_json, '$.timeline_id'),'-','')=NEW.timeline_id"
        )
        scope += (
            " OR NOT EXISTS (SELECT 1 FROM asset_versions v WHERE v.id=NEW.asset_version_id "
            "AND replace(json_extract(v.metadata_json,'$.subtitle_production_id'),'-','')=NEW.id "
            "AND replace(json_extract(v.metadata_json,'$.dependency_manifest_id'),'-','')"
            "=NEW.dependency_manifest_id)"
        )
        for action in ("UPDATE", "DELETE", "INSERT"):
            condition = (
                f" WHEN ({scope}) OR NOT COALESCE(({shape}),0)" if action == "INSERT" else ""
            )
            op.execute(
                f"CREATE TRIGGER immutable_subtitle_productions_{action.lower()} BEFORE {action} "
                f"ON subtitle_productions{condition} BEGIN SELECT RAISE(ABORT, "
                "'IMMUTABLE_HISTORY: subtitle production'); END"
            )
    else:
        shape = (
            "jsonb_typeof(NEW.snapshot_json->'cues')='array' "
            "AND jsonb_array_length(NEW.snapshot_json->'cues') BETWEEN 1 AND 256 "
            "AND NEW.snapshot_json->>'schema_version'='subtitle-track/1' "
            "AND NEW.snapshot_json->>'episode_id'=NEW.episode_id::text "
            "AND NEW.snapshot_json->>'timeline_id'=NEW.timeline_id::text"
        )
        scope += (
            " OR NOT EXISTS (SELECT 1 FROM asset_versions v WHERE v.id=NEW.asset_version_id "
            "AND v.metadata_json->>'subtitle_production_id'=NEW.id::text "
            "AND v.metadata_json->>'dependency_manifest_id'=NEW.dependency_manifest_id::text)"
        )
        op.execute(
            "CREATE FUNCTION immutable_subtitle_productions_guard() "
            "RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: subtitle production'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_subtitle_productions_write BEFORE UPDATE OR DELETE "
            "ON subtitle_productions FOR EACH ROW "
            "EXECUTE FUNCTION immutable_subtitle_productions_guard()"
        )
        op.execute(
            "CREATE TRIGGER immutable_subtitle_productions_truncate BEFORE TRUNCATE "
            "ON subtitle_productions FOR EACH STATEMENT "
            "EXECUTE FUNCTION immutable_subtitle_productions_guard()"
        )
        op.execute(
            "CREATE FUNCTION immutable_subtitle_productions_insert_guard() "
            "RETURNS trigger LANGUAGE plpgsql "
            f"AS $$ BEGIN IF ({scope}) OR NOT COALESCE(({shape}),false) THEN RAISE EXCEPTION "
            "'IMMUTABLE_HISTORY: subtitle scope'; END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_subtitle_productions_insert "
            "BEFORE INSERT ON subtitle_productions "
            "FOR EACH ROW EXECUTE FUNCTION immutable_subtitle_productions_insert_guard()"
        )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Subtitle downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE subtitle_productions IN ACCESS EXCLUSIVE MODE")
    if db.scalar(sa.text("SELECT 1 FROM subtitle_productions LIMIT 1")):
        raise RuntimeError("Cannot discard subtitle production history")
    op.drop_table("subtitle_productions")
    if db.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_subtitle_productions_guard()")
        op.execute("DROP FUNCTION immutable_subtitle_productions_insert_guard()")
