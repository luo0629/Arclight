"""Supplier presets, rotatable credential envelopes and account audit.

Revision ID: 0041
Revises: 0040
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def entity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        "service_key_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("active_key_id", sa.String(16), nullable=False),
    )
    op.execute("INSERT INTO service_key_state (id, active_key_id) VALUES (1, '')")
    op.create_table(
        "service_credentials",
        *entity(),
        sa.Column("encrypted_key", sa.String(8192), nullable=False),
        sa.Column("key_id", sa.String(16), nullable=False),
        sa.Column("legacy_digest", sa.String(64), unique=True),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    with op.batch_alter_table("model_services") as batch:
        batch.add_column(
            sa.Column("supplier", sa.String(40), nullable=False, server_default="custom")
        )
        batch.add_column(
            sa.Column("supplier_name", sa.String(100), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column(
                "response_format", sa.String(20), nullable=False, server_default="json_schema"
            )
        )
        batch.add_column(
            sa.Column(
                "credential_id",
                sa.Uuid(),
                sa.ForeignKey(
                    "service_credentials.id",
                    name="fk_model_services_credential_id_service_credentials",
                ),
            )
        )
    with op.batch_alter_table("model_services") as batch:
        for column in ("supplier", "supplier_name", "response_format"):
            batch.alter_column(column, server_default=None)
    op.create_table(
        "service_audit",
        *entity(),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column(
            "details_json",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
    )
    op.create_index("ix_service_audit_user_id", "service_audit", ["user_id"])
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_service_audit_{action.lower()} BEFORE {action} "
                "ON service_audit BEGIN "
                "SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: service audit'); END"
            )
        op.execute(
            "CREATE TRIGGER immutable_service_audit_replace BEFORE INSERT ON service_audit "
            "WHEN EXISTS (SELECT 1 FROM service_audit WHERE id=NEW.id) "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: service audit'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION immutable_service_audit_guard() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: service audit'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_service_audit_write BEFORE UPDATE OR DELETE "
            "ON service_audit FOR EACH ROW EXECUTE FUNCTION immutable_service_audit_guard()"
        )
        op.execute(
            "CREATE TRIGGER immutable_service_audit_truncate BEFORE TRUNCATE "
            "ON service_audit FOR EACH STATEMENT EXECUTE FUNCTION immutable_service_audit_guard()"
        )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Credential downgrade requires online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE service_audit, service_credentials, model_services, service_key_state "
            "IN ACCESS EXCLUSIVE MODE"
        )
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if any(
        connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
        for table in ("service_audit", "service_credentials")
    ):
        raise RuntimeError("Service credential or audit history exists; cannot discard")
    if connection.scalar(
        sa.text(
            "SELECT 1 FROM model_services WHERE supplier <> 'custom' OR supplier_name <> '' "
            "OR response_format <> 'json_schema' LIMIT 1"
        )
    ):
        raise RuntimeError("Supplier configuration exists; cannot discard")
    op.drop_table("service_audit")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_service_audit_guard()")
    with op.batch_alter_table("model_services") as batch:
        batch.drop_constraint(
            "fk_model_services_credential_id_service_credentials", type_="foreignkey"
        )
        for column in ("credential_id", "response_format", "supplier_name", "supplier"):
            batch.drop_column(column)
    op.drop_table("service_credentials")
    op.drop_table("service_key_state")
