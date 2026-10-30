"""Durable media delivery identity and exclusive active asset generation."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

ACTIVE = (
    "target_asset_id IS NOT NULL AND status IN "
    "('CANCEL_REQUESTED','CREATED','MANUAL_REVIEW','RECONCILING','RESULT_UNKNOWN',"
    "'RETRY_WAIT','RUNNING','SUBMITTED','SUBMITTING')"
)
FROZEN = "id created_at job_id expected_revision request_fingerprint"
CLAIM = "attempt_id provider_supports_idempotency provider_idempotency_key"
OUTCOME = "updated_at status receipt_json finished_at"


def upgrade() -> None:
    connection = op.get_bind()
    duplicate_sql = (
        f"SELECT target_asset_id FROM generation_jobs WHERE {ACTIVE} "
        "GROUP BY target_asset_id HAVING COUNT(*) > 1"
    )
    if connection.dialect.name == "postgresql":
        op.execute("LOCK TABLE generation_jobs IN ACCESS EXCLUSIVE MODE")
    elif not context.is_offline_mode():
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if context.is_offline_mode():
        if connection.dialect.name != "postgresql":
            raise RuntimeError("Active generation migration requires an online preservation check")
        op.execute(f"""DO $$ BEGIN IF EXISTS ({duplicate_sql}) THEN
            RAISE EXCEPTION
                'Concurrent active generation exists; resolve explicitly before upgrade';
            END IF; END; $$""")
    elif connection.execute(sa.text(duplicate_sql)).first() is not None:
        raise RuntimeError("Concurrent active generation exists; resolve explicitly before upgrade")

    op.create_index(
        "uq_generation_active_asset",
        "generation_jobs",
        ["target_asset_id"],
        unique=True,
        sqlite_where=sa.text(ACTIVE),
        postgresql_where=sa.text(ACTIVE),
    )
    op.create_table(
        "generation_dispatches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("generation_attempts.id")),
        sa.Column("provider_supports_idempotency", sa.Boolean()),
        sa.Column("provider_idempotency_key", sa.String(160)),
        sa.Column("receipt_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("job_id", "expected_revision", name="uq_generation_dispatch_revision"),
        sa.CheckConstraint("expected_revision >= 0", name="generation_dispatch_revision"),
        sa.CheckConstraint(
            "status IN ('READY','CLAIMED','FINISHED','SKIPPED')", name="generation_dispatch_status"
        ),
        sa.CheckConstraint(
            "(status IN ('READY','CLAIMED') AND finished_at IS NULL AND receipt_json IS NULL) "
            "OR (status IN ('FINISHED','SKIPPED') "
            "AND finished_at IS NOT NULL AND receipt_json IS NOT NULL)",
            name="generation_dispatch_completion",
        ),
        sa.CheckConstraint(
            "(status IN ('READY','SKIPPED') AND attempt_id IS NULL "
            "AND provider_supports_idempotency IS NULL AND provider_idempotency_key IS NULL) "
            "OR (status IN ('CLAIMED','FINISHED') AND attempt_id IS NOT NULL "
            "AND provider_supports_idempotency IS NOT NULL "
            "AND ((provider_supports_idempotency AND provider_idempotency_key IS NOT NULL) "
            "OR (NOT provider_supports_idempotency AND provider_idempotency_key IS NULL)))",
            name="generation_dispatch_claim",
        ),
    )
    for column in ("job_id", "attempt_id"):
        op.create_index(f"ix_generation_dispatches_{column}", "generation_dispatches", [column])
    sqlite = connection.dialect.name == "sqlite"
    operator = "IS NOT" if sqlite else "IS DISTINCT FROM"

    def changed(names):
        return " OR ".join(f'OLD."{name}" {operator} NEW."{name}"' for name in names.split())

    condition = (
        f"({changed(FROZEN)}) OR (OLD.status <> 'READY' AND ({changed(CLAIM)})) "
        f"OR (OLD.finished_at IS NOT NULL AND ({changed(OUTCOME)})) "
        "OR (NEW.status <> OLD.status AND NOT "
        "((OLD.status = 'READY' AND NEW.status IN ('CLAIMED','SKIPPED')) "
        "OR (OLD.status = 'CLAIMED' AND NEW.status = 'FINISHED')))"
    )
    if sqlite:
        op.execute(f"""CREATE TRIGGER immutable_generation_dispatches_update
            BEFORE UPDATE ON generation_dispatches WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: generation_dispatches'); END""")
        op.execute("""CREATE TRIGGER immutable_generation_dispatches_delete
            BEFORE DELETE ON generation_dispatches
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: generation_dispatches'); END""")
        op.execute("""CREATE TRIGGER immutable_generation_dispatches_insert
            BEFORE INSERT ON generation_dispatches
            WHEN EXISTS (SELECT 1 FROM generation_dispatches WHERE id = NEW.id OR
                (job_id = NEW.job_id AND expected_revision = NEW.expected_revision))
            BEGIN SELECT RAISE(ABORT,
                'IMMUTABLE_HISTORY: generation_dispatches replacement'); END""")
    else:
        op.execute(f"""CREATE FUNCTION immutable_generation_dispatches_guard()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
                    RAISE EXCEPTION 'IMMUTABLE_HISTORY: generation_dispatches'
                        USING ERRCODE = '23514';
                END IF;
                IF {condition} THEN
                    RAISE EXCEPTION 'IMMUTABLE_HISTORY: generation_dispatches'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END; $$""")
        op.execute("""CREATE TRIGGER immutable_generation_dispatches_write
            BEFORE UPDATE OR DELETE ON generation_dispatches
            FOR EACH ROW EXECUTE FUNCTION immutable_generation_dispatches_guard()""")
        op.execute("""CREATE TRIGGER immutable_generation_dispatches_truncate
            BEFORE TRUNCATE ON generation_dispatches
            FOR EACH STATEMENT EXECUTE FUNCTION immutable_generation_dispatches_guard()""")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Generation history downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE dependency_manifests, generation_jobs, generation_attempts, "
            "generation_reconciliations, generation_dispatches IN ACCESS EXCLUSIVE MODE"
        )
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    for table in (
        "generation_dispatches",
        "generation_reconciliations",
        "generation_attempts",
        "generation_jobs",
    ):
        if connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")):
            raise RuntimeError(
                "Generation history exists; preserve cancellation history and delivery facts"
            )
    if connection.scalar(
        sa.text("SELECT 1 FROM dependency_manifests WHERE target_asset_id IS NULL LIMIT 1")
    ):
        raise RuntimeError("Generic media manifests exist; preserve nullable target history")
    if connection.dialect.name == "postgresql":
        op.execute("DROP TRIGGER immutable_generation_dispatches_write ON generation_dispatches")
        op.execute("DROP TRIGGER immutable_generation_dispatches_truncate ON generation_dispatches")
        op.execute("DROP FUNCTION immutable_generation_dispatches_guard()")
    op.drop_table("generation_dispatches")
    op.drop_index("uq_generation_active_asset", table_name="generation_jobs")
