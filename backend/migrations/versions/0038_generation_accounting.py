"""Reserve and settle generation budgets without altering historical attempts."""

import sqlalchemy as sa
from alembic import context, op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

HISTORY = {
    "generation_budget_reservations": (
        "id=NEW.id OR (job_id=NEW.job_id AND attempt_no=NEW.attempt_no AND sequence=NEW.sequence)"
    ),
    "generation_budget_settlements": "id=NEW.id OR reservation_id=NEW.reservation_id",
}


def backfill() -> None:
    # Copy evidence into the new ledger; never infer that a failed paid call was free.
    bind = op.get_bind()
    identity = sa.text(
        "gen_random_uuid()" if bind.dialect.name == "postgresql" else "lower(hex(randomblob(16)))"
    )
    reservations = sa.table(
        "generation_budget_reservations",
        sa.column("id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("job_id", sa.Uuid()),
        sa.column("attempt_no", sa.Integer()),
        sa.column("sequence", sa.Integer()),
        sa.column("amount", sa.Numeric(18, 6)),
        sa.column("currency", sa.String()),
        sa.column("source", sa.String()),
    )
    attempts = sa.table(
        "generation_attempts",
        sa.column("id", sa.Uuid()),
        sa.column("job_id", sa.Uuid()),
        sa.column("attempt_no", sa.Integer()),
        sa.column("cost", sa.Numeric(18, 6)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("finished_at", sa.DateTime(timezone=True)),
    )
    quotes = sa.table(
        "generation_budget_quotes",
        sa.column("job_id", sa.Uuid()),
        sa.column("maximum_cost", sa.Numeric(18, 6)),
        sa.column("currency", sa.String()),
    )
    jobs = sa.table(
        "generation_jobs",
        sa.column("id", sa.Uuid()),
        sa.column("status", sa.String()),
        sa.column("attempt_count", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    columns = [column.name for column in reservations.c]
    op.execute(
        reservations.insert().from_select(
            columns,
            sa.select(
                identity,
                attempts.c.created_at,
                attempts.c.updated_at,
                attempts.c.job_id,
                attempts.c.attempt_no,
                sa.literal(1),
                quotes.c.maximum_cost,
                quotes.c.currency,
                sa.literal("LEGACY"),
            ).select_from(attempts.join(quotes, quotes.c.job_id == attempts.c.job_id)),
        )
    )
    op.execute(
        reservations.insert().from_select(
            columns,
            sa.select(
                identity,
                jobs.c.created_at,
                jobs.c.updated_at,
                jobs.c.id,
                jobs.c.attempt_count + 1,
                sa.literal(1),
                quotes.c.maximum_cost,
                quotes.c.currency,
                sa.literal("LEGACY"),
            )
            .select_from(jobs.join(quotes, quotes.c.job_id == jobs.c.id))
            .where(jobs.c.status.in_(["CREATED", "RETRY_WAIT"])),
        )
    )
    settlements = sa.table(
        "generation_budget_settlements",
        sa.column("id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("reservation_id", sa.Uuid()),
        sa.column("cost", sa.Numeric(18, 6)),
        sa.column("source", sa.String()),
        sa.column("evidence_reference", sa.String()),
        sa.column("created_by", sa.String()),
    )
    facts = sa.table(
        "generation_reconciliations",
        sa.column("attempt_id", sa.Uuid()),
        sa.column("disposition", sa.String()),
        sa.column("receipt_json", sa.JSON()),
    )
    not_accepted = sa.exists(
        sa.select(1)
        .select_from(facts)
        .where(
            facts.c.attempt_id == attempts.c.id,
            facts.c.disposition == "APPLIED",
            facts.c.receipt_json["status"].as_string() == "NOT_ACCEPTED",
        )
    )
    op.execute(
        settlements.insert().from_select(
            [column.name for column in settlements.c],
            sa.select(
                identity,
                attempts.c.updated_at,
                attempts.c.updated_at,
                reservations.c.id,
                sa.func.coalesce(attempts.c.cost, 0),
                sa.case(
                    (attempts.c.cost.is_not(None), "LEGACY"),
                    (not_accepted, "NOT_ACCEPTED"),
                    else_="FREE",
                ),
                sa.null(),
                sa.literal("budget-migration"),
            )
            .select_from(
                reservations.join(
                    attempts,
                    sa.and_(
                        reservations.c.job_id == attempts.c.job_id,
                        reservations.c.attempt_no == attempts.c.attempt_no,
                    ),
                )
            )
            .where(
                sa.or_(
                    attempts.c.cost.is_not(None),
                    sa.and_(reservations.c.amount == 0, attempts.c.finished_at.is_not(None)),
                    not_accepted,
                )
            ),
        )
    )


def upgrade() -> None:
    op.create_table(
        "generation_budget_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.UniqueConstraint(
            "job_id", "attempt_no", "sequence", name="uq_budget_reservation_attempt"
        ),
        sa.CheckConstraint("attempt_no > 0", name="budget_reservation_attempt"),
        sa.CheckConstraint("sequence > 0", name="budget_reservation_sequence"),
        sa.CheckConstraint("amount >= 0", name="budget_reservation_amount"),
        sa.CheckConstraint("currency = 'USD'", name="budget_reservation_currency"),
        sa.CheckConstraint(
            "source IN ('ADMISSION', 'RETRY', 'LEGACY')", name="budget_reservation_source"
        ),
    )
    op.create_index(
        "ix_generation_budget_reservations_job_id", "generation_budget_reservations", ["job_id"]
    )
    op.create_table(
        "generation_budget_settlements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "reservation_id",
            sa.Uuid(),
            sa.ForeignKey("generation_budget_reservations.id"),
            nullable=False,
        ),
        sa.Column("cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("evidence_reference", sa.String(200)),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("reservation_id"),
        sa.CheckConstraint("cost >= 0", name="budget_settlement_cost"),
        sa.CheckConstraint(
            "source IN ('PROVIDER', 'NOT_ACCEPTED', 'PRE_SUBMISSION', 'FREE', 'MANUAL', 'LEGACY')",
            name="budget_settlement_source",
        ),
    )
    backfill()
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION immutable_generation_accounting_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'IMMUTABLE_HISTORY: generation accounting' USING ERRCODE='23514';
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
                "FOR EACH ROW EXECUTE FUNCTION immutable_generation_accounting_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION immutable_generation_accounting_guard()"
            )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Accounting downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("LOCK TABLE " + ", ".join(HISTORY) + " IN ACCESS EXCLUSIVE MODE")
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if any(connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in HISTORY):
        raise RuntimeError("Generation history exists; cannot discard budget accounting")
    op.drop_table("generation_budget_settlements")
    op.drop_index(
        "ix_generation_budget_reservations_job_id", table_name="generation_budget_reservations"
    )
    op.drop_table("generation_budget_reservations")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_generation_accounting_guard()")
