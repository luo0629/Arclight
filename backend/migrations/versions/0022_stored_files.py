"""Immutable file metadata; no rewrite or relocation of historical bytes."""

import sqlalchemy as sa
from alembic import context, op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stored_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("asset_version_id", sa.Uuid(), sa.ForeignKey("asset_versions.id"), unique=True),
        sa.Column("storage_backend", sa.String(16), nullable=False),
        sa.Column("bucket", sa.String(63)),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("original_name", sa.String(160), nullable=False),
        sa.Column("source_note", sa.Text(), nullable=False),
        sa.Column("rights_note", sa.Text(), nullable=False),
        sa.Column("rights_confirmed", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("storage_backend", "object_key", name="uq_stored_file_object"),
        sa.CheckConstraint("byte_size > 0", name="stored_file_size"),
    )
    op.create_index("ix_stored_files_project_id", "stored_files", ["project_id"])
    if op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(f"""CREATE TRIGGER immutable_stored_files_{operation.lower()}
                BEFORE {operation} ON stored_files
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: stored_files'); END""")
        op.execute("""CREATE TRIGGER immutable_stored_files_insert
            BEFORE INSERT ON stored_files WHEN EXISTS (
                SELECT 1 FROM stored_files WHERE id=NEW.id
                OR (storage_backend=NEW.storage_backend AND object_key=NEW.object_key)
                OR (asset_version_id IS NOT NULL AND asset_version_id=NEW.asset_version_id))
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: stored_files replacement'); END""")
    else:
        op.execute("""CREATE FUNCTION immutable_stored_files_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'IMMUTABLE_HISTORY: stored_files' USING ERRCODE='23514';
            END; $$""")
        op.execute("""CREATE TRIGGER immutable_stored_files_write BEFORE UPDATE OR DELETE
            ON stored_files FOR EACH ROW EXECUTE FUNCTION immutable_stored_files_guard()""")
        op.execute("""CREATE TRIGGER immutable_stored_files_truncate BEFORE TRUNCATE
            ON stored_files FOR EACH STATEMENT EXECUTE FUNCTION immutable_stored_files_guard()""")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("File metadata downgrade requires online data check")
    if op.get_bind().execute(sa.text("SELECT 1 FROM stored_files LIMIT 1")).first():
        raise RuntimeError("Cannot remove saved file metadata")
    if op.get_bind().dialect.name != "sqlite":
        op.execute("DROP FUNCTION immutable_stored_files_guard() CASCADE")
    op.drop_table("stored_files")
