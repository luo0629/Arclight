"""Protect immutable history from direct SQL, replacement and truncation."""

import sqlalchemy as sa
from alembic import context, op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

# Frozen migration contract: later model edits must not alter historical DDL.
# Only lifecycle/control columns are omitted. Every table also protects id/created_at.
FIELDS = {
    "scripts": "episode_id run_id version_no content_json",
    "storyboards": "episode_id run_id script_version_id version_no content_json",
    "shots": "storyboard_id shot_no duration_ms description content_json",
    "asset_versions": (
        "asset_id version_no object_key checksum metadata_json created_by creation_key"
    ),
    "visual_assets": "project_id asset_type creation_source created_by",
    "story_sources": (
        "episode_id import_no import_key request_fingerprint original_name source_type "
        "media_type encoding byte_size sha256 raw_bytes text source_note rights_note created_by "
        "updated_at"
    ),
    "story_source_revisions": (
        "source_id root_source_id parent_source_id version_no change_note origin_kind "
        "rights_status storage_backend object_key updated_at"
    ),
    "story_source_bindings": "source_id run_id updated_at",
    "story_analyses": (
        "episode_id run_id version_no source_id source_name source_sha256 story_sha256 "
        "created_by content_json updated_at"
    ),
    "story_outlines": (
        "project_id episode_id run_id base_version_id version_no content_json "
        "source_snapshot created_by save_key request_fingerprint"
    ),
    "visual_bible_versions": (
        "project_id version_no art_style line_style render_style palette lighting "
        "composition prohibited negative_prompt locked_fields legacy_snapshot "
        "change_kind created_by creation_key request_fingerprint updated_at"
    ),
    "asset_dependencies": (
        "source_asset_version_id target_asset_version_id dependency_type updated_at"
    ),
    "character_confirmations": (
        "project_id script_id analysis_id source_name entry_id request_fingerprint "
        "snapshot_json created_by updated_at"
    ),
    "workflow_runs": (
        "episode_id workflow_type workflow_version trace_id idempotency_key "
        "request_fingerprint input_json"
    ),
    "reviews": "run_id resource_type version_id reviewer_id decision comment updated_at",
}

# SQLite REPLACE can bypass DELETE triggers when recursive_triggers is OFF.
# Detect collisions on every unique identity BEFORE INSERT, including nullable keys.
UNIQUE_KEYS = {
    "scripts": [("episode_id", "version_no"), ("run_id",)],
    "storyboards": [("episode_id", "version_no"), ("run_id",)],
    "shots": [("storyboard_id", "shot_no")],
    "asset_versions": [("asset_id", "version_no"), ("asset_id", "creation_key")],
    "story_sources": [("episode_id", "import_no"), ("episode_id", "import_key")],
    "story_source_revisions": [
        ("source_id",),
        ("root_source_id", "version_no"),
        ("parent_source_id",),
        ("object_key",),
    ],
    "story_source_bindings": [("run_id",)],
    "story_analyses": [("episode_id", "version_no"), ("run_id",)],
    "story_outlines": [("project_id", "version_no"), ("project_id", "save_key"), ("run_id",)],
    "visual_bible_versions": [("project_id", "version_no"), ("project_id", "creation_key")],
    "asset_dependencies": [
        ("source_asset_version_id", "target_asset_version_id", "dependency_type")
    ],
    "character_confirmations": [("script_id", "source_name"), ("analysis_id", "source_name")],
    "workflow_runs": [("episode_id", "idempotency_key")],
    "reviews": [("run_id", "resource_type", "version_id")],
}


def upgrade() -> None:
    connection = op.get_bind()
    dialect = connection.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError("Immutable history requires SQLite or PostgreSQL")
    if dialect == "sqlite" and not context.is_offline_mode():
        _reserve_sqlite_writer(connection)
    for table, names in FIELDS.items():
        columns = ["id", "created_at", *names.split()]
        label = f"immutable_{table}"
        if dialect == "sqlite":
            changed = " OR ".join(f'OLD."{name}" IS NOT NEW."{name}"' for name in columns)
            op.execute(f'''CREATE TRIGGER {label}_update BEFORE UPDATE ON "{table}"
                FOR EACH ROW WHEN {changed}
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END''')
            op.execute(f'''CREATE TRIGGER {label}_delete BEFORE DELETE ON "{table}"
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END''')
            groups = [("id",), *UNIQUE_KEYS.get(table, [])]
            conflicts = " OR ".join(
                "("
                + " AND ".join(
                    f'NEW."{name}" IS NOT NULL AND existing."{name}" = NEW."{name}"'
                    for name in group
                )
                + ")"
                for group in groups
            )
            op.execute(f'''CREATE TRIGGER {label}_insert BEFORE INSERT ON "{table}"
                WHEN EXISTS (SELECT 1 FROM "{table}" AS existing WHERE {conflicts})
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table} replacement'); END''')
        else:
            changed = " OR ".join(f'OLD."{name}" IS DISTINCT FROM NEW."{name}"' for name in columns)
            op.execute(f"""CREATE FUNCTION {label}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
                        RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE = '23514';
                    END IF;
                    IF {changed} THEN
                        RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END; $$""")
            op.execute(f'''CREATE TRIGGER {label}_write BEFORE UPDATE OR DELETE ON "{table}"
                FOR EACH ROW EXECUTE FUNCTION {label}_guard()''')
            op.execute(f'''CREATE TRIGGER {label}_truncate BEFORE TRUNCATE ON "{table}"
                FOR EACH STATEMENT EXECUTE FUNCTION {label}_guard()''')


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Immutable history downgrade requires an online preservation check")
    connection = op.get_bind()
    # Keep the empty-history check and trigger removal in one exclusive write window.
    # Otherwise a concurrent insert could acquire history after the check passed.
    if connection.dialect.name == "sqlite":
        _reserve_sqlite_writer(connection)
    else:
        tables = ", ".join(f'"{name}"' for name in FIELDS)
        connection.execute(sa.text(f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE"))
    # No switch, environment variable or session flag can silently unprotect existing history.
    if any(connection.scalar(sa.text(f'SELECT 1 FROM "{table}" LIMIT 1')) for table in FIELDS):
        raise RuntimeError(
            "Immutable version history exists; refusing to remove database protection"
        )
    for table in reversed(FIELDS):
        label = f"immutable_{table}"
        if connection.dialect.name == "sqlite":
            for operation in ("update", "delete", "insert"):
                op.execute(f"DROP TRIGGER {label}_{operation}")
        else:
            op.execute(f'DROP TRIGGER {label}_write ON "{table}"')
            op.execute(f'DROP TRIGGER {label}_truncate ON "{table}"')
            op.execute(f"DROP FUNCTION {label}_guard()")


def _reserve_sqlite_writer(connection: sa.engine.Connection) -> None:
    # Python's legacy SQLite transaction mode does not BEGIN for DDL or SELECT.
    # Reuse a transaction opened by earlier revisions, or reserve before guard DDL.
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
