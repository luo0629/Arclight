"""Durable, write-once query/manual reconciliation receipts."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

FROZEN = "id created_at job_id attempt_id claimed_revision source actor evidence_reference"
OUTCOME = "updated_at receipt_json disposition finished_at"


def upgrade() -> None:
    op.create_table(
        "generation_reconciliations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("generation_attempts.id"), nullable=False),
        sa.Column("claimed_revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("evidence_reference", sa.String(200)),
        sa.Column("receipt_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "job_id", "claimed_revision", name="uq_generation_reconciliation_revision"
        ),
        sa.CheckConstraint("claimed_revision >= 0", name="generation_reconciliation_revision"),
        sa.CheckConstraint(
            "source IN ('PROVIDER', 'MANUAL')", name="generation_reconciliation_source"
        ),
        sa.CheckConstraint(
            "(disposition = 'PENDING' AND finished_at IS NULL AND receipt_json IS NULL) "
            "OR (disposition IN ('APPLIED', 'SUPERSEDED') "
            "AND finished_at IS NOT NULL AND receipt_json IS NOT NULL)",
            name="generation_reconciliation_completion",
        ),
    )
    for column in ("job_id", "attempt_id"):
        op.create_index(
            f"ix_generation_reconciliations_{column}", "generation_reconciliations", [column]
        )
    sqlite = op.get_bind().dialect.name == "sqlite"
    operator = "IS NOT" if sqlite else "IS DISTINCT FROM"

    def changed(names):
        return " OR ".join(f'OLD."{name}" {operator} NEW."{name}"' for name in names.split())

    condition = f"({changed(FROZEN)}) OR (OLD.finished_at IS NOT NULL AND ({changed(OUTCOME)}))"
    if sqlite:
        op.execute(f"""CREATE TRIGGER immutable_generation_reconciliations_update
            BEFORE UPDATE ON generation_reconciliations WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: generation_reconciliations'); END""")
        op.execute("""CREATE TRIGGER immutable_generation_reconciliations_delete
            BEFORE DELETE ON generation_reconciliations
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: generation_reconciliations'); END""")
        op.execute("""CREATE TRIGGER immutable_generation_reconciliations_insert
            BEFORE INSERT ON generation_reconciliations
            WHEN EXISTS (SELECT 1 FROM generation_reconciliations WHERE id = NEW.id OR
                (job_id = NEW.job_id AND claimed_revision = NEW.claimed_revision))
            BEGIN SELECT RAISE(ABORT,
                'IMMUTABLE_HISTORY: generation_reconciliations replacement'); END""")
    else:
        op.execute(f"""CREATE FUNCTION immutable_generation_reconciliations_guard()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
                    RAISE EXCEPTION 'IMMUTABLE_HISTORY: generation_reconciliations'
                        USING ERRCODE = '23514';
                END IF;
                IF {condition} THEN
                    RAISE EXCEPTION 'IMMUTABLE_HISTORY: generation_reconciliations'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END; $$""")
        op.execute("""CREATE TRIGGER immutable_generation_reconciliations_write
            BEFORE UPDATE OR DELETE ON generation_reconciliations
            FOR EACH ROW EXECUTE FUNCTION immutable_generation_reconciliations_guard()""")
        op.execute("""CREATE TRIGGER immutable_generation_reconciliations_truncate
            BEFORE TRUNCATE ON generation_reconciliations
            FOR EACH STATEMENT EXECUTE FUNCTION immutable_generation_reconciliations_guard()""")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Generation history downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE dependency_manifests, generation_jobs, generation_attempts, "
            "generation_reconciliations IN ACCESS EXCLUSIVE MODE"
        )
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    for table in ("generation_reconciliations", "generation_attempts", "generation_jobs"):
        if connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")):
            raise RuntimeError(
                "Generation history exists; preserve cancellation history and reconciliation facts"
            )
    if connection.scalar(
        sa.text("SELECT 1 FROM dependency_manifests WHERE target_asset_id IS NULL LIMIT 1")
    ):
        raise RuntimeError("Generic media manifests exist; preserve nullable target history")
    if connection.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER immutable_generation_reconciliations_write ON generation_reconciliations"
        )
        op.execute(
            "DROP TRIGGER immutable_generation_reconciliations_truncate "
            "ON generation_reconciliations"
        )
        op.execute("DROP FUNCTION immutable_generation_reconciliations_guard()")
    op.drop_table("generation_reconciliations")
