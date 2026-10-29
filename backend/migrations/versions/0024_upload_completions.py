"""Immutable upload validation and exact asset registration receipts."""

import sqlalchemy as sa
from alembic import context, op

revision = "0024"
down_revision = "0023"
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
        "upload_completions",
        *common(),
        sa.Column(
            "upload_id", sa.Uuid(), sa.ForeignKey("upload_intents.id"), nullable=False, unique=True
        ),
        sa.Column(
            "file_id", sa.Uuid(), sa.ForeignKey("stored_files.id"), nullable=False, unique=True
        ),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
    )
    op.create_table(
        "file_registrations",
        *common(),
        sa.Column("file_id", sa.Uuid(), sa.ForeignKey("stored_files.id"), nullable=False),
        sa.Column(
            "asset_version_id",
            sa.Uuid(),
            sa.ForeignKey("asset_versions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("created_by", sa.String(100), nullable=False),
    )
    op.create_index("ix_file_registrations_file_id", "file_registrations", ["file_id"])
    for table, unique_columns in (
        ("upload_completions", ("upload_id", "file_id")),
        ("file_registrations", ("asset_version_id",)),
    ):
        if op.get_bind().dialect.name == "sqlite":
            for operation in ("UPDATE", "DELETE"):
                op.execute(f"""CREATE TRIGGER immutable_{table}_{operation.lower()}
                    BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END""")
            predicate = " OR ".join(f"{col}=NEW.{col}" for col in ("id", *unique_columns))
            op.execute(f"""CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table}
                WHEN EXISTS (SELECT 1 FROM {table} WHERE {predicate})
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table} replacement'); END""")
        else:
            op.execute(f"""CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE
                ON {table} FOR EACH ROW EXECUTE FUNCTION immutable_stored_files_guard()""")
            op.execute(f"""CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE
                ON {table} FOR EACH STATEMENT EXECUTE FUNCTION immutable_stored_files_guard()""")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Upload completion downgrade requires online data check")
    for table in ("upload_completions", "file_registrations"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Cannot remove saved upload validation or registration")
    op.drop_table("file_registrations")
    op.drop_table("upload_completions")
