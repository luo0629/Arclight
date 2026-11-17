"""Immutable complete story-ledger snapshots, approval receipts and a CAS head.

This migration freezes its own schema and guards; do not import current models.
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
HISTORY = ("story_ledger_versions", "story_ledger_approvals")


def entity_columns():
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def immutable_guard(table, invalid):
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_{table}_{operation.lower()} "
                f"BEFORE {operation} ON {table} BEGIN "
                "SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: story ledger'); END"
            )
        op.execute(
            f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
            f"WHEN {invalid} BEGIN "
            "SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: story ledger identity or scope'); END"
        )
    else:
        op.execute(
            f"CREATE FUNCTION immutable_{table}_guard() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: story ledger'; END; $$"
        )
        for name, event, level in (
            ("write", "UPDATE OR DELETE", "ROW"),
            ("truncate", "TRUNCATE", "STATEMENT"),
        ):
            op.execute(
                f"CREATE TRIGGER immutable_{table}_{name} BEFORE {event} ON {table} "
                f"FOR EACH {level} EXECUTE FUNCTION immutable_{table}_guard()"
            )
        op.execute(
            f"CREATE FUNCTION immutable_{table}_insert_guard() RETURNS trigger LANGUAGE plpgsql "
            f"AS $$ BEGIN IF {invalid} THEN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: story ledger identity or scope'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION immutable_{table}_insert_guard()"
        )


def head_guard():
    invalid = (
        "(NEW.latest_version_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM story_ledger_versions v WHERE v.id=NEW.latest_version_id "
        "AND v.project_id=NEW.project_id)) OR "
        "((NEW.approved_version_id IS NULL) <> (NEW.approval_id IS NULL)) OR "
        "(NEW.approval_id IS NOT NULL AND (NEW.latest_version_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM story_ledger_approvals a WHERE a.id=NEW.approval_id "
        "AND a.project_id=NEW.project_id AND a.version_id=NEW.approved_version_id)))"
    )
    collision = (
        "EXISTS (SELECT 1 FROM story_ledger_heads h WHERE h.id=NEW.id "
        "OR h.project_id=NEW.project_id)"
    )
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        op.execute(
            "CREATE TRIGGER story_ledger_head_delete BEFORE DELETE ON story_ledger_heads "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: story ledger head'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION story_ledger_head_preserve_guard() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: story ledger head'; END; $$"
        )
        for name, event, level in (
            ("delete", "DELETE", "ROW"),
            ("truncate", "TRUNCATE", "STATEMENT"),
        ):
            op.execute(
                f"CREATE TRIGGER story_ledger_head_{name} BEFORE {event} ON story_ledger_heads "
                f"FOR EACH {level} EXECUTE FUNCTION story_ledger_head_preserve_guard()"
            )
    changed = (
        "NEW.id IS NOT OLD.id OR NEW.project_id IS NOT OLD.project_id "
        "OR NEW.created_at IS NOT OLD.created_at OR NEW.revision<>OLD.revision+1"
        if sqlite
        else "NEW.id IS DISTINCT FROM OLD.id OR NEW.project_id IS DISTINCT FROM OLD.project_id "
        "OR NEW.created_at IS DISTINCT FROM OLD.created_at OR NEW.revision<>OLD.revision+1"
    )
    if sqlite:
        for operation, condition in (
            ("insert", f"({invalid}) OR ({collision})"),
            ("update", f"({invalid}) OR ({changed})"),
        ):
            op.execute(
                f"CREATE TRIGGER story_ledger_head_{operation} BEFORE {operation.upper()} "
                f"ON story_ledger_heads WHEN {condition} BEGIN "
                "SELECT RAISE(ABORT, 'STORY_LEDGER_HEAD_CONFLICT'); END"
            )
    else:
        for operation, condition in (
            ("insert", f"({invalid}) OR ({collision})"),
            ("update", f"({invalid}) OR ({changed})"),
        ):
            op.execute(
                f"CREATE FUNCTION story_ledger_head_{operation}_guard() RETURNS trigger "
                f"LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN "
                "RAISE EXCEPTION 'STORY_LEDGER_HEAD_CONFLICT'; "
                "END IF; RETURN NEW; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER story_ledger_head_{operation} BEFORE {operation.upper()} "
                f"ON story_ledger_heads FOR EACH ROW "
                f"EXECUTE FUNCTION story_ledger_head_{operation}_guard()"
            )


def upgrade():
    op.create_table(
        "story_ledger_versions",
        *entity_columns(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("base_version_id", sa.Uuid(), sa.ForeignKey("story_ledger_versions.id")),
        sa.Column("creation_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("content_json", JSON, nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("source_manifest_json", JSON, nullable=False),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("receipt_json", JSON, nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "version_no", name="uq_story_ledger_version"),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_story_ledger_creation"),
        sa.CheckConstraint(
            "version_no > 0", name=op.f("ck_story_ledger_versions_story_ledger_version_positive")
        ),
    )
    op.create_index("ix_story_ledger_versions_project_id", "story_ledger_versions", ["project_id"])
    op.create_table(
        "story_ledger_approvals",
        *entity_columns(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column(
            "version_id", sa.Uuid(), sa.ForeignKey("story_ledger_versions.id"), nullable=False
        ),
        sa.Column("creation_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("receipt_json", JSON, nullable=False),
        sa.Column("approved_by", sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", name="uq_story_ledger_approval_version"),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_story_ledger_approval_creation"),
    )
    op.create_index(
        "ix_story_ledger_approvals_project_id", "story_ledger_approvals", ["project_id"]
    )
    op.create_table(
        "story_ledger_heads",
        *entity_columns(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latest_version_id", sa.Uuid(), sa.ForeignKey("story_ledger_versions.id")),
        sa.Column("approved_version_id", sa.Uuid(), sa.ForeignKey("story_ledger_versions.id")),
        sa.Column("approval_id", sa.Uuid(), sa.ForeignKey("story_ledger_approvals.id")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_story_ledger_heads_project_id"),
        sa.CheckConstraint(
            "revision >= 0", name=op.f("ck_story_ledger_heads_story_ledger_head_revision")
        ),
    )
    immutable_guard(
        "story_ledger_versions",
        "EXISTS (SELECT 1 FROM story_ledger_versions v WHERE v.id=NEW.id OR "
        "(v.project_id=NEW.project_id AND (v.version_no=NEW.version_no "
        "OR v.creation_key=NEW.creation_key))) OR "
        "(NEW.base_version_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM story_ledger_versions v WHERE v.id=NEW.base_version_id "
        "AND v.project_id=NEW.project_id))",
    )
    immutable_guard(
        "story_ledger_approvals",
        "EXISTS (SELECT 1 FROM story_ledger_approvals a WHERE a.id=NEW.id "
        "OR a.version_id=NEW.version_id OR "
        "(a.project_id=NEW.project_id AND a.creation_key=NEW.creation_key)) OR "
        "NOT EXISTS (SELECT 1 FROM story_ledger_versions v WHERE v.id=NEW.version_id "
        "AND v.project_id=NEW.project_id AND v.content_sha256=NEW.content_sha256 "
        "AND v.source_manifest_sha256=NEW.source_manifest_sha256)",
    )
    head_guard()


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Story ledger downgrade requires an online history check")
    db = op.get_bind()
    sqlite = db.dialect.name == "sqlite"
    if sqlite:
        driver = db.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            db.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        op.execute(
            "LOCK TABLE story_ledger_heads, story_ledger_approvals, story_ledger_versions "
            "IN ACCESS EXCLUSIVE MODE"
        )
    for table in (*HISTORY, "story_ledger_heads"):
        if db.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")):
            raise RuntimeError("Cannot discard story ledger history")
    op.drop_table("story_ledger_heads")
    op.drop_table("story_ledger_approvals")
    op.drop_table("story_ledger_versions")
    if not sqlite:
        for table in HISTORY:
            op.execute(f"DROP FUNCTION immutable_{table}_guard()")
            op.execute(f"DROP FUNCTION immutable_{table}_insert_guard()")
        for operation in ("insert", "update"):
            op.execute(f"DROP FUNCTION story_ledger_head_{operation}_guard()")
        op.execute("DROP FUNCTION story_ledger_head_preserve_guard()")
