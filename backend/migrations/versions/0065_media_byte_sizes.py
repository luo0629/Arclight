"""Widen media receipts without rebuilding append-only history tables."""

import sqlite3
from contextlib import closing

import sqlalchemy as sa
from alembic import context, op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

MEDIA_TABLES = (
    "cleanup_items",
    "render_artifacts",
    "render_inspections",
    "render_report_links",
    "stored_files",
    "upload_intents",
)
OLD_INTEGER_MIN = -(2**31)
OLD_INTEGER_MAX = 2**31 - 1
OLD_INSPECTION_MAX = 134217728
SIZE_CONSTRAINT = "ck_render_inspections_render_inspection_size"
OLD_CHECK = "byte_size > 0 AND byte_size <= 134217728"
NEW_CHECK = "byte_size > 0"


def sqlite_inspection_check(*, restore: bool) -> None:
    """Change only the named CHECK; preserve table identity, rows, FKs and triggers."""
    if context.is_offline_mode():
        raise RuntimeError("Media byte size SQLite migration requires an online schema check")
    db = op.get_bind()
    original = db.scalar(
        sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name='render_inspections'")
    )
    before, after = (NEW_CHECK, OLD_CHECK) if restore else (OLD_CHECK, NEW_CHECK)
    old_constraint = f"CONSTRAINT {SIZE_CONSTRAINT} CHECK ({before})"
    new_constraint = f"CONSTRAINT {SIZE_CONSTRAINT} CHECK ({after})"
    if not isinstance(original, str) or original.count(old_constraint) != 1:
        raise RuntimeError("Unexpected render inspection CHECK; refusing a schema rewrite")
    replacement = original.replace(old_constraint, new_constraint, 1)
    # Validate the entire replacement DDL before enabling SQLite's schema editor.
    # SQLite INTEGER already stores signed 64-bit values, so no column/table copy
    # is needed. The documented CHECK-only schema edit preserves immutable guards.
    with closing(sqlite3.connect(":memory:")) as validation:
        validation.execute(replacement)
    schema_version = db.exec_driver_sql("PRAGMA schema_version").scalar_one()
    writable_schema = db.exec_driver_sql("PRAGMA writable_schema").scalar_one()
    if writable_schema:
        raise RuntimeError("SQLite writable_schema must be disabled before migration")
    db.exec_driver_sql("PRAGMA writable_schema=ON")
    try:
        changed = db.execute(
            sa.text(
                "UPDATE sqlite_master SET sql=:replacement WHERE type='table' "
                "AND name='render_inspections' AND sql=:original"
            ),
            {"replacement": replacement, "original": original},
        )
        if changed.rowcount != 1:
            raise RuntimeError("Render inspection schema changed during migration")
        db.exec_driver_sql(f"PRAGMA schema_version={schema_version + 1}")
    finally:
        db.exec_driver_sql("PRAGMA writable_schema=OFF")
    if db.exec_driver_sql("PRAGMA integrity_check").scalars().all() != ["ok"]:
        raise RuntimeError("Media byte size schema edit failed SQLite integrity validation")


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        sqlite_inspection_check(restore=False)
        return
    for table in MEDIA_TABLES:
        op.alter_column(
            table,
            "byte_size",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )
    op.drop_constraint(op.f(SIZE_CONSTRAINT), "render_inspections", type_="check")
    op.create_check_constraint(op.f(SIZE_CONSTRAINT), "render_inspections", NEW_CHECK)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Media byte size downgrade requires an online range check")
    db = op.get_bind()
    sqlite = db.dialect.name == "sqlite"
    if not sqlite:
        op.execute(f"LOCK TABLE {', '.join(MEDIA_TABLES)} IN ACCESS EXCLUSIVE MODE")
    # Check every affected receipt before the first DDL, including restoring the
    # older inspection CHECK. Never truncate, rewrite or discard accepted history.
    for table in MEDIA_TABLES:
        if db.scalar(
            sa.text(
                f"SELECT 1 FROM {table} WHERE byte_size < :minimum OR byte_size > :maximum LIMIT 1"
            ),
            {"minimum": OLD_INTEGER_MIN, "maximum": OLD_INTEGER_MAX},
        ):
            raise RuntimeError(f"Cannot narrow media byte size history: {table}")
    if db.scalar(
        sa.text("SELECT 1 FROM render_inspections WHERE byte_size > :maximum LIMIT 1"),
        {"maximum": OLD_INSPECTION_MAX},
    ):
        raise RuntimeError("Cannot restore the old render inspection byte size limit")
    if sqlite:
        sqlite_inspection_check(restore=True)
        return
    op.drop_constraint(op.f(SIZE_CONSTRAINT), "render_inspections", type_="check")
    op.create_check_constraint(op.f(SIZE_CONSTRAINT), "render_inspections", OLD_CHECK)
    for table in MEDIA_TABLES:
        op.alter_column(
            table,
            "byte_size",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
