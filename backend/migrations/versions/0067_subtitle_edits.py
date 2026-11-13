"""Allow immutable subtitle display edits with exact parent provenance."""

import sqlalchemy as sa
from alembic import context, op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def insert_scope() -> str:
    # Preserve the complete 0066 insertion scope; the remaining immutable
    # triggers and the production table are deliberately left in place.
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
        return scope + (
            " OR NOT EXISTS (SELECT 1 FROM asset_versions v WHERE v.id=NEW.asset_version_id "
            "AND replace(json_extract(v.metadata_json,'$.subtitle_production_id'),'-','')=NEW.id "
            "AND replace(json_extract(v.metadata_json,'$.dependency_manifest_id'),'-','')"
            "=NEW.dependency_manifest_id)"
        )
    return scope + (
        " OR NOT EXISTS (SELECT 1 FROM asset_versions v WHERE v.id=NEW.asset_version_id "
        "AND v.metadata_json->>'subtitle_production_id'=NEW.id::text "
        "AND v.metadata_json->>'dependency_manifest_id'=NEW.dependency_manifest_id::text)"
    )


def sqlite_edits() -> str:
    return (
        "json_extract(NEW.snapshot_json,'$.schema_version')='subtitle-track/2' "
        "AND json_type(NEW.snapshot_json,'$.display_cues')='array' "
        "AND json_array_length(NEW.snapshot_json,'$.display_cues')="
        "json_array_length(NEW.snapshot_json,'$.cues') "
        "AND EXISTS (SELECT 1 FROM subtitle_productions p "
        "JOIN asset_versions pv ON pv.id=p.asset_version_id "
        "JOIN asset_versions v ON v.id=NEW.asset_version_id "
        "WHERE p.asset_version_id="
        "replace(json_extract(NEW.snapshot_json,'$.edit_source_version_id'),'-','') "
        "AND p.episode_id=NEW.episode_id AND p.timeline_id=NEW.timeline_id "
        "AND pv.asset_id=v.asset_id AND pv.version_no<v.version_no "
        "AND pv.checksum=json_extract(NEW.snapshot_json,'$.edit_source_checksum') "
        "AND json_extract(pv.metadata_json,'$.subtitle_document_sha256')="
        "json_extract(NEW.snapshot_json,'$.edit_source_document_sha256') "
        "AND json_extract(p.snapshot_json,'$.timeline_sha256')="
        "json_extract(NEW.snapshot_json,'$.timeline_sha256') "
        "AND json_extract(p.snapshot_json,'$.cues')="
        "json_extract(NEW.snapshot_json,'$.cues')) "
        "AND NOT EXISTS (SELECT 1 FROM json_each(NEW.snapshot_json,'$.display_cues') c "
        "LEFT JOIN json_each(NEW.snapshot_json,'$.display_cues') previous "
        "ON CAST(previous.key AS INTEGER)=CAST(c.key AS INTEGER)-1 "
        "WHERE NOT COALESCE((json_type(c.value,'$.cue_no')='integer' "
        "AND json_extract(c.value,'$.cue_no')=CAST(c.key AS INTEGER)+1 "
        "AND json_type(c.value,'$.text')='text' "
        "AND length(trim(json_extract(c.value,'$.text'))) BETWEEN 1 AND 2000 "
        "AND json_type(c.value,'$.start_ms')='integer' "
        "AND json_type(c.value,'$.end_ms')='integer' "
        "AND json_extract(c.value,'$.start_ms')>=0 "
        "AND json_extract(c.value,'$.end_ms')>json_extract(c.value,'$.start_ms') "
        "AND json_extract(c.value,'$.end_ms')<="
        "(SELECT duration_ms FROM timelines WHERE id=NEW.timeline_id) "
        "AND json_extract(c.value,'$.start_ms')>="
        "COALESCE(json_extract(previous.value,'$.end_ms'),0)),0))"
    )


def postgres_edits() -> str:
    return (
        "NEW.snapshot_json->>'schema_version'='subtitle-track/2' "
        "AND CASE WHEN jsonb_typeof(NEW.snapshot_json->'display_cues')='array' THEN "
        "jsonb_array_length(NEW.snapshot_json->'display_cues')="
        "jsonb_array_length(NEW.snapshot_json->'cues') "
        "AND NOT EXISTS (SELECT 1 FROM "
        "jsonb_array_elements(NEW.snapshot_json->'display_cues') "
        "WITH ORDINALITY c(value, ordinal) "
        "LEFT JOIN jsonb_array_elements(NEW.snapshot_json->'display_cues') "
        "WITH ORDINALITY previous(value, ordinal) ON previous.ordinal=c.ordinal-1 "
        "WHERE NOT COALESCE((CASE WHEN "
        "jsonb_typeof(c.value->'cue_no')='number' "
        "AND c.value->>'cue_no' ~ '^[1-9][0-9]*$' "
        "AND jsonb_typeof(c.value->'start_ms')='number' "
        "AND c.value->>'start_ms' ~ '^(0|[1-9][0-9]*)$' "
        "AND jsonb_typeof(c.value->'end_ms')='number' "
        "AND c.value->>'end_ms' ~ '^[1-9][0-9]*$' THEN "
        "(c.value->>'cue_no')::numeric=c.ordinal "
        "AND jsonb_typeof(c.value->'text')='string' "
        "AND length(btrim(c.value->>'text')) BETWEEN 1 AND 2000 "
        "AND (c.value->>'end_ms')::numeric>(c.value->>'start_ms')::numeric "
        "AND (c.value->>'end_ms')::numeric<="
        "(SELECT duration_ms FROM timelines WHERE id=NEW.timeline_id) "
        "AND (c.value->>'start_ms')::numeric>=COALESCE("
        "CASE WHEN previous.value->>'end_ms' ~ '^[1-9][0-9]*$' "
        "THEN (previous.value->>'end_ms')::numeric END,0) "
        "ELSE false END),false)) ELSE false END "
        "AND EXISTS (SELECT 1 FROM subtitle_productions p "
        "JOIN asset_versions pv ON pv.id=p.asset_version_id "
        "JOIN asset_versions v ON v.id=NEW.asset_version_id "
        "WHERE p.asset_version_id::text=NEW.snapshot_json->>'edit_source_version_id' "
        "AND p.episode_id=NEW.episode_id AND p.timeline_id=NEW.timeline_id "
        "AND pv.asset_id=v.asset_id AND pv.version_no<v.version_no "
        "AND pv.checksum=NEW.snapshot_json->>'edit_source_checksum' "
        "AND pv.metadata_json->>'subtitle_document_sha256'="
        "NEW.snapshot_json->>'edit_source_document_sha256' "
        "AND p.snapshot_json->'timeline_sha256'=NEW.snapshot_json->'timeline_sha256' "
        "AND p.snapshot_json->'cues'=NEW.snapshot_json->'cues')"
    )


def install_insert_guard(*, allow_edits: bool) -> None:
    scope = insert_scope()
    if op.get_bind().dialect.name == "sqlite":
        version = "json_extract(NEW.snapshot_json,'$.schema_version')='subtitle-track/1'"
        if allow_edits:
            version = f"({version} OR ({sqlite_edits()}))"
        shape = (
            "json_type(NEW.snapshot_json,'$.cues')='array' "
            "AND json_array_length(NEW.snapshot_json,'$.cues') BETWEEN 1 AND 256 "
            f"AND {version} "
            "AND replace(json_extract(NEW.snapshot_json,'$.episode_id'),'-','')=NEW.episode_id "
            "AND replace(json_extract(NEW.snapshot_json,'$.timeline_id'),'-','')=NEW.timeline_id"
        )
        op.execute("DROP TRIGGER immutable_subtitle_productions_insert")
        op.execute(
            "CREATE TRIGGER immutable_subtitle_productions_insert BEFORE INSERT "
            f"ON subtitle_productions WHEN ({scope}) OR NOT COALESCE(({shape}),0) "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: subtitle production'); END"
        )
    else:
        version = "NEW.snapshot_json->>'schema_version'='subtitle-track/1'"
        if allow_edits:
            version = f"({version} OR ({postgres_edits()}))"
        shape = (
            "jsonb_typeof(NEW.snapshot_json->'cues')='array' "
            "AND jsonb_array_length(NEW.snapshot_json->'cues') BETWEEN 1 AND 256 "
            f"AND {version} "
            "AND NEW.snapshot_json->>'episode_id'=NEW.episode_id::text "
            "AND NEW.snapshot_json->>'timeline_id'=NEW.timeline_id::text"
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION immutable_subtitle_productions_insert_guard() "
            "RETURNS trigger LANGUAGE plpgsql "
            f"AS $$ BEGIN IF ({scope}) OR NOT COALESCE(({shape}),false) THEN RAISE EXCEPTION "
            "'IMMUTABLE_HISTORY: subtitle scope'; END IF; RETURN NEW; END; $$"
        )


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("LOCK TABLE subtitle_productions IN ACCESS EXCLUSIVE MODE")
    install_insert_guard(allow_edits=True)


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Subtitle edit downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE subtitle_productions IN ACCESS EXCLUSIVE MODE")
        query = (
            "SELECT 1 FROM subtitle_productions "
            "WHERE snapshot_json->>'schema_version'='subtitle-track/2' LIMIT 1"
        )
    else:
        query = (
            "SELECT 1 FROM subtitle_productions "
            "WHERE json_extract(snapshot_json,'$.schema_version')='subtitle-track/2' LIMIT 1"
        )
    if db.scalar(sa.text(query)):
        raise RuntimeError("Cannot discard subtitle edit history")
    install_insert_guard(allow_edits=False)
