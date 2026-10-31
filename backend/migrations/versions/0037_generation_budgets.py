"""Persist admission-time generation budget policy and quotes."""

import sqlalchemy as sa
from alembic import context, op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None
HISTORY = {
    "generation_budget_batches": (
        "id=NEW.id OR (project_id=NEW.project_id AND creation_key=NEW.creation_key)"
    ),
    "generation_budget_quotes": "id=NEW.id OR job_id=NEW.job_id",
    "generation_budget_adjustments": (
        "id=NEW.id OR (project_id=NEW.project_id AND creation_key=NEW.creation_key)"
    ),
    "generation_budget_decisions": "id=NEW.id OR adjustment_id=NEW.adjustment_id",
}


def upgrade() -> None:
    op.create_table(
        "generation_budget_policies",
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("task_limit", sa.Numeric(18, 6)),
        sa.Column("episode_limit", sa.Numeric(18, 6)),
        sa.Column("batch_limit", sa.Numeric(18, 6)),
        sa.Column("warning_percent", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("max_candidates_per_target", sa.Integer(), nullable=False, server_default="64"),
        sa.CheckConstraint("revision >= 0", name="budget_policy_revision"),
        sa.CheckConstraint("warning_percent BETWEEN 1 AND 100", name="budget_warning_percent"),
        sa.CheckConstraint(
            "max_candidates_per_target BETWEEN 1 AND 10000", name="budget_candidates"
        ),
        sa.CheckConstraint(
            "(task_limit IS NULL OR task_limit >= 0) AND "
            "(episode_limit IS NULL OR episode_limit >= 0) AND "
            "(batch_limit IS NULL OR batch_limit >= 0)",
            name="budget_policy_limits",
        ),
    )
    op.create_table(
        "generation_budget_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id")),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("budget_limit", sa.Numeric(18, 6), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_budget_batch_key"),
        sa.CheckConstraint("budget_limit >= 0", name="budget_batch_limit"),
    )
    op.create_index(
        "ix_generation_budget_batches_project_id", "generation_budget_batches", ["project_id"]
    )
    op.create_table(
        "generation_budget_quotes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False, unique=True
        ),
        sa.Column("batch_id", sa.Uuid(), sa.ForeignKey("generation_budget_batches.id")),
        sa.Column("maximum_cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("pricing_version", sa.String(100), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("maximum_cost >= 0", name="budget_quote_cost"),
        sa.CheckConstraint("currency = 'USD'", name="budget_quote_currency"),
        sa.CheckConstraint("candidate_count BETWEEN 1 AND 64", name="budget_quote_candidates"),
    )
    op.create_table(
        "generation_budget_adjustments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("requested_limit", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_budget_adjustment_key"),
        sa.CheckConstraint("requested_limit >= 0", name="budget_adjustment_limit"),
    )
    op.create_index(
        "ix_generation_budget_adjustments_project_id",
        "generation_budget_adjustments",
        ["project_id"],
    )
    op.create_table(
        "generation_budget_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "adjustment_id",
            sa.Uuid(),
            sa.ForeignKey("generation_budget_adjustments.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("decided_by", sa.String(100), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION immutable_generation_budget_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'IMMUTABLE_HISTORY: generation budget' USING ERRCODE='23514';
            END; $$""")
    for table, duplicate in HISTORY.items():
        if op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END"
                )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {duplicate}) "
                f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table} replacement'); END"
            )
        else:
            op.execute(
                f"CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION immutable_generation_budget_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION immutable_generation_budget_guard()"
            )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Budget downgrade requires an online preservation check")
    connection = op.get_bind()
    tables = ["generation_budget_policies", *HISTORY]
    if connection.dialect.name == "postgresql":
        op.execute("LOCK TABLE " + ", ".join(tables) + " IN ACCESS EXCLUSIVE MODE")
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if any(connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in tables):
        raise RuntimeError("Generation history exists; cannot discard budget governance")
    op.drop_table("generation_budget_decisions")
    op.drop_index(
        "ix_generation_budget_adjustments_project_id", table_name="generation_budget_adjustments"
    )
    op.drop_table("generation_budget_adjustments")
    op.drop_table("generation_budget_quotes")
    op.drop_index("ix_generation_budget_batches_project_id", table_name="generation_budget_batches")
    op.drop_table("generation_budget_batches")
    op.drop_table("generation_budget_policies")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_generation_budget_guard()")
