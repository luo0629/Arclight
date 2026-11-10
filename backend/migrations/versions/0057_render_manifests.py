"""Append-only render inputs, artifacts and exact report links."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

TABLES = ("render_manifests", "render_artifacts", "render_report_links")
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def columns():
    return (
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade():
    op.create_table(
        "render_manifests",
        *columns(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("timeline_id", sa.Uuid(), sa.ForeignKey("timelines.id"), nullable=False),
        sa.Column(
            "dependency_manifest_id",
            sa.Uuid(),
            sa.ForeignKey("dependency_manifests.id"),
            nullable=False,
        ),
        sa.Column("font_file_id", sa.Uuid(), sa.ForeignKey("stored_files.id")),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_json", JSON, nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_render_manifest_creation"),
    )
    for field in ("project_id", "timeline_id"):
        op.create_index(f"ix_render_manifests_{field}", "render_manifests", [field])
    for table, parent_field, parent in (
        ("render_artifacts", "manifest_id", "render_manifests"),
        ("render_report_links", "artifact_id", "render_artifacts"),
    ):
        op.create_table(
            table,
            *columns(),
            sa.Column(
                parent_field, sa.Uuid(), sa.ForeignKey(f"{parent}.id"), nullable=False, unique=True
            ),
            sa.Column(
                "file_id", sa.Uuid(), sa.ForeignKey("stored_files.id"), nullable=False, unique=True
            ),
            sa.Column("object_key", sa.String(512), nullable=False),
            sa.Column("checksum", sa.String(64), nullable=False),
            sa.Column("byte_size", sa.Integer(), nullable=False),
            *(
                [
                    sa.Column("duration_ms", sa.Integer(), nullable=False),
                    sa.CheckConstraint(
                        "duration_ms > 0 AND duration_ms <= 3600000",
                        name="render_artifact_duration",
                    ),
                ]
                if table == "render_artifacts"
                else []
            ),
            sa.CheckConstraint(
                "byte_size > 0",
                name="render_artifact_size"
                if table == "render_artifacts"
                else "render_report_size",
            ),
        )
    conditions = {
        "render_manifests": (
            "NOT EXISTS (SELECT 1 FROM timelines t JOIN episodes e ON e.id=t.episode_id "
            "JOIN dependency_outputs o ON o.asset_version_id=t.id "
            "JOIN dependency_manifests d ON d.id=o.dependency_manifest_id "
            "WHERE t.id=NEW.timeline_id AND t.sealed AND e.project_id=NEW.project_id "
            "AND d.project_id=NEW.project_id AND d.id=NEW.dependency_manifest_id) "
            "OR (NEW.font_file_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stored_files f "
            "WHERE f.id=NEW.font_file_id AND f.project_id=NEW.project_id AND f.rights_confirmed "
            "AND f.media_type IN ('font/ttf','font/otf'))) "
            "OR EXISTS (SELECT 1 FROM render_manifests WHERE id=NEW.id "
            "OR (project_id=NEW.project_id AND creation_key=NEW.creation_key))"
        ),
        "render_artifacts": (
            "NOT EXISTS (SELECT 1 FROM render_manifests m JOIN stored_files f "
            "ON f.project_id=m.project_id WHERE m.id=NEW.manifest_id AND f.id=NEW.file_id "
            "AND f.media_type='video/mp4' "
            "AND f.object_key=NEW.object_key AND f.checksum=NEW.checksum "
            "AND f.byte_size=NEW.byte_size) "
            "OR EXISTS (SELECT 1 FROM render_artifacts WHERE id=NEW.id "
            "OR manifest_id=NEW.manifest_id OR file_id=NEW.file_id)"
        ),
        "render_report_links": (
            "NOT EXISTS (SELECT 1 FROM render_artifacts a "
            "JOIN render_manifests m ON m.id=a.manifest_id "
            "JOIN stored_files f ON f.project_id=m.project_id WHERE a.id=NEW.artifact_id "
            "AND f.id=NEW.file_id AND f.media_type='application/json' "
            "AND f.object_key=NEW.object_key AND f.checksum=NEW.checksum "
            "AND f.byte_size=NEW.byte_size) "
            "OR EXISTS (SELECT 1 FROM render_report_links WHERE id=NEW.id "
            "OR artifact_id=NEW.artifact_id OR file_id=NEW.file_id)"
        ),
    }
    sqlite = op.get_bind().dialect.name == "sqlite"
    if not sqlite:
        op.execute(
            "CREATE FUNCTION immutable_render_history_guard() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: render history'; END; $$"
        )
    for table in TABLES:
        if sqlite:
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{operation.lower()} "
                    f"BEFORE {operation} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render history'); END"
                )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"WHEN {conditions[table]} "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render scope or replacement'); END"
            )
        else:
            op.execute(
                f"CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION immutable_render_history_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION immutable_render_history_guard()"
            )
            op.execute(
                f"CREATE FUNCTION immutable_{table}_insert_guard() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF {conditions[table]} THEN "
                "RAISE EXCEPTION 'IMMUTABLE_HISTORY: render scope or replacement'; "
                "END IF; RETURN NEW; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION immutable_{table}_insert_guard()"
            )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Render manifest downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE render_manifests, render_artifacts, render_report_links "
            "IN ACCESS EXCLUSIVE MODE"
        )
    else:
        driver = db.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            db.exec_driver_sql("BEGIN IMMEDIATE")
    if any(db.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        raise RuntimeError("Cannot discard render manifest history")
    for table in reversed(TABLES):
        op.drop_table(table)
        if db.dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION immutable_{table}_insert_guard()")
    if db.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_render_history_guard()")
