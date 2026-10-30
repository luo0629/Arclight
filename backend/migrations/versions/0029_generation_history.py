"""Generic media manifests and database-enforced immutable attempt history."""

import sqlalchemy as sa
from alembic import context, op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

# Frozen migration definitions: do not import mutable application models.
MANIFEST_FIELDS = (
    "id created_at updated_at project_id target_asset_id creation_key request_fingerprint "
    "snapshot_json created_by"
)
JOB_FIELDS = (
    "id created_at project_id episode_id target_type target_asset_id input_manifest_id "
    "task_type provider model idempotency_key request_fingerprint expected_asset_revision "
    "expected_lock_version expected_current_version_id created_by"
)
ATTEMPT_FIELDS = (
    "id created_at job_id attempt_no input_manifest_id request_fingerprint provider model "
    "input_snapshot_json submitted_at"
)
OUTCOME_FIELDS = (
    "updated_at provider_request_id status result_json error_code latency_ms cost finished_at"
)
UNIQUE = {
    "dependency_manifests": [("project_id", "creation_key")],
    "generation_jobs": [("project_id", "idempotency_key")],
    "generation_attempts": [("job_id", "attempt_no"), ("provider", "provider_request_id")],
}


def drop_guard(table: str) -> None:
    label = f"immutable_{table}"
    if op.get_bind().dialect.name == "sqlite":
        for kind in ("update", "delete", "insert"):
            op.execute(f"DROP TRIGGER {label}_{kind}")
    else:
        op.execute(f'DROP TRIGGER {label}_write ON "{table}"')
        op.execute(f'DROP TRIGGER {label}_truncate ON "{table}"')
        op.execute(f"DROP FUNCTION {label}_guard()")


def create_guard(table: str, fields: str) -> None:
    label = f"immutable_{table}"
    sqlite = op.get_bind().dialect.name == "sqlite"

    def changed(names: str) -> str:
        operator = "IS NOT" if sqlite else "IS DISTINCT FROM"
        return " OR ".join(f'OLD."{name}" {operator} NEW."{name}"' for name in names.split())

    condition = changed(fields)
    if table == "generation_attempts":
        condition += (
            f" OR (OLD.provider_request_id IS NOT NULL AND ({changed('provider_request_id')}))"
            f" OR (OLD.finished_at IS NOT NULL AND ({changed(OUTCOME_FIELDS)}))"
        )
    if sqlite:
        op.execute(f'''CREATE TRIGGER {label}_update BEFORE UPDATE ON "{table}"
            WHEN {condition} BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END''')
        op.execute(f'''CREATE TRIGGER {label}_delete BEFORE DELETE ON "{table}"
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END''')
        identities = [("id",), *UNIQUE[table]]
        collisions = " OR ".join(
            "("
            + " AND ".join(
                f'NEW."{name}" IS NOT NULL AND existing."{name}" = NEW."{name}"' for name in group
            )
            + ")"
            for group in identities
        )
        op.execute(f'''CREATE TRIGGER {label}_insert BEFORE INSERT ON "{table}"
            WHEN EXISTS (SELECT 1 FROM "{table}" AS existing WHERE {collisions})
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table} replacement'); END''')
    else:
        op.execute(f"""CREATE FUNCTION {label}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
                    RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE = '23514';
                END IF;
                IF {condition} THEN
                    RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END; $$""")
        op.execute(f'''CREATE TRIGGER {label}_write BEFORE UPDATE OR DELETE ON "{table}"
            FOR EACH ROW EXECUTE FUNCTION {label}_guard()''')
        op.execute(f'''CREATE TRIGGER {label}_truncate BEFORE TRUNCATE ON "{table}"
            FOR EACH STATEMENT EXECUTE FUNCTION {label}_guard()''')


def lock_schema() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        op.execute(
            "LOCK TABLE dependency_manifests, generation_jobs, generation_attempts "
            "IN ACCESS EXCLUSIVE MODE"
        )


def alter_manifest(nullable: bool) -> None:
    drop_guard("dependency_manifests")
    with op.batch_alter_table("dependency_manifests") as batch:
        batch.alter_column("target_asset_id", existing_type=sa.Uuid(), nullable=nullable)
    create_guard("dependency_manifests", MANIFEST_FIELDS)


def upgrade() -> None:
    lock_schema()
    alter_manifest(True)
    create_guard("generation_jobs", JOB_FIELDS)
    create_guard("generation_attempts", ATTEMPT_FIELDS)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Generation history downgrade requires an online preservation check")
    lock_schema()
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT 1 FROM generation_jobs LIMIT 1")) or connection.scalar(
        sa.text("SELECT 1 FROM generation_attempts LIMIT 1")
    ):
        raise RuntimeError(
            "Generation history exists; preserve cancellation history and media inputs"
        )
    if connection.scalar(
        sa.text("SELECT 1 FROM dependency_manifests WHERE target_asset_id IS NULL LIMIT 1")
    ):
        raise RuntimeError("Generic media manifests exist; preserve nullable target history")
    for table in ("generation_attempts", "generation_jobs"):
        drop_guard(table)
    alter_manifest(False)
