"""Preserve exact human review requests, decisions and resume receipts.

Revision ID: 0049
Revises: 0048
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TABLES = ("human_review_requests", "human_review_decisions", "human_review_resumes")


def identity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        TABLES[0],
        *identity(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id")),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id")),
        sa.Column("creation_key", sa.String(160), nullable=False),
        sa.Column("handler", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_json", JSON, nullable=False),
        sa.UniqueConstraint("project_id", "creation_key"),
    )
    for column in ("project_id", "run_id"):
        op.create_index(f"ix_human_review_requests_{column}", TABLES[0], [column])
    op.create_table(
        TABLES[1],
        *identity(),
        sa.Column("request_id", sa.Uuid(), sa.ForeignKey(TABLES[0] + ".id"), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("decision_json", JSON, nullable=False),
        sa.UniqueConstraint("request_id"),
    )
    op.create_table(
        TABLES[2],
        *identity(),
        sa.Column("request_id", sa.Uuid(), sa.ForeignKey(TABLES[0] + ".id"), nullable=False),
        sa.Column("decision_id", sa.Uuid(), sa.ForeignKey(TABLES[1] + ".id"), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("result_json", JSON, nullable=False),
        sa.UniqueConstraint("request_id"),
        sa.UniqueConstraint("decision_id"),
    )
    for table in TABLES:
        if op.get_bind().dialect.name == "sqlite":
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} "
                    f"ON {table} BEGIN "
                    "SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: human review'); END"
                )
            conflict = (
                " OR (project_id=NEW.project_id AND creation_key=NEW.creation_key)"
                if table == TABLES[0]
                else " OR request_id=NEW.request_id"
            )
            if table == TABLES[2]:
                conflict += " OR decision_id=NEW.decision_id"
            op.execute(
                f"CREATE TRIGGER immutable_{table}_replace BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE id=NEW.id{conflict}) "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: human review'); END"
            )
        else:
            op.execute(
                f"CREATE FUNCTION immutable_{table}_guard() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: human review'; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION immutable_{table}_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION immutable_{table}_guard()"
            )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Human review downgrade needs an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE " + ", ".join(TABLES) + " IN ACCESS EXCLUSIVE MODE")
    if any(db.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        raise RuntimeError("Human review history exists; cannot discard it")
    for table in reversed(TABLES):
        op.drop_table(table)
        if db.dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION immutable_{table}_guard()")
