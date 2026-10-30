"""Bounded cleanup plans preserve target identities and diagnostic history."""

import sqlalchemy as sa
from alembic import context, op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    def common():
        return [
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        ]

    op.create_table(
        "cleanup_plans",
        *common(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("namespace", sa.String(32), nullable=False),
        sa.Column("storage_identity", sa.String(64), nullable=False),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execute_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_cursor", sa.Text()),
        sa.Column("scanned_count", sa.Integer(), nullable=False),
        sa.Column("retained_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
    )
    op.create_index("ix_cleanup_plans_project_id", "cleanup_plans", ["project_id"])
    op.create_table(
        "cleanup_items",
        *common(),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("cleanup_plans.id"), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("object_token", sa.String(256), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("plan_id", "object_key", name="uq_cleanup_plan_object"),
        sa.CheckConstraint("byte_size >= 0 AND attempt_count >= 0", name="cleanup_nonnegative"),
        sa.CheckConstraint(
            "status IN ('PLANNED','RUNNING','DELETED','ABSENT','RETAINED','FAILED')",
            name="cleanup_status",
        ),
    )
    op.create_index("ix_cleanup_items_plan_id", "cleanup_items", ["plan_id"])
    for table, columns in (
        ("cleanup_plans", None),
        (
            "cleanup_items",
            "id,plan_id,object_key,byte_size,modified_at,object_token,reason,created_at",
        ),
    ):
        if op.get_bind().dialect.name == "sqlite":
            update = "UPDATE" if columns is None else f"UPDATE OF {columns}"
            for operation in (update, "DELETE"):
                suffix = operation.split()[0].lower()
                op.execute(f"""CREATE TRIGGER immutable_{table}_{suffix}
                    BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table} target'); END""")
            op.execute(f"""CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table}
                WHEN EXISTS (SELECT 1 FROM {table} WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: cleanup replacement'); END""")
        else:
            update = "UPDATE" if columns is None else f"UPDATE OF {columns}"
            op.execute(f"""CREATE TRIGGER immutable_{table}_write
                BEFORE {update} OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION immutable_stored_files_guard()""")
            op.execute(f"""CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION immutable_stored_files_guard()""")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Cleanup downgrade requires online data check")
    if op.get_bind().execute(sa.text("SELECT 1 FROM cleanup_plans LIMIT 1")).first():
        raise RuntimeError("Cannot remove saved cleanup diagnostic history")
    op.drop_table("cleanup_items")
    op.drop_table("cleanup_plans")
