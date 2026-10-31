"""Preserve exact renderer input, intermediate lifecycle and output receipts."""

import sqlalchemy as sa
from alembic import context, op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "render_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("input_file_id", sa.Uuid(), sa.ForeignKey("stored_files.id"), nullable=False),
        sa.Column("retry_of_id", sa.Uuid(), sa.ForeignKey("render_executions.id")),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("storage_identity", sa.String(64), nullable=False),
        sa.Column("intermediate_key", sa.String(512), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("output_file_id", sa.Uuid(), sa.ForeignKey("stored_files.id")),
        sa.Column("error_code", sa.String(64)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="render_status"
        ),
    )
    op.create_index("ix_render_executions_project_id", "render_executions", ["project_id"])
    frozen = (
        "id,created_at,project_id,input_file_id,retry_of_id,created_by,"
        "storage_identity,intermediate_key"
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"""CREATE TRIGGER immutable_render_executions_update
            BEFORE UPDATE OF {frozen} ON render_executions
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render input'); END""")
        op.execute("""CREATE TRIGGER immutable_render_executions_delete
            BEFORE DELETE ON render_executions
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render history'); END""")
        op.execute("""CREATE TRIGGER immutable_render_executions_insert
            BEFORE INSERT ON render_executions
            WHEN EXISTS (SELECT 1 FROM render_executions WHERE id=NEW.id)
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render replacement'); END""")
    else:
        op.execute(f"""CREATE TRIGGER immutable_render_executions_write
            BEFORE UPDATE OF {frozen} OR DELETE ON render_executions
            FOR EACH ROW EXECUTE FUNCTION immutable_stored_files_guard()""")
        op.execute("""CREATE TRIGGER immutable_render_executions_truncate
            BEFORE TRUNCATE ON render_executions
            FOR EACH STATEMENT EXECUTE FUNCTION immutable_stored_files_guard()""")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Renderer downgrade requires an online history check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql("LOCK TABLE render_executions IN ACCESS EXCLUSIVE MODE")
    if connection.scalar(sa.text("SELECT 1 FROM render_executions LIMIT 1")):
        raise RuntimeError("Cannot remove saved renderer execution history")
    op.drop_table("render_executions")
