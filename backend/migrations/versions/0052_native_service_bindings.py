"""Native protocol selection, private job credentials and validated media receipts."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None
TABLES = ("generation_service_bindings", "generation_media_artifacts")
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def history_guard(table: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: native service'); END"
            )
        conflict = (
            "job_id=NEW.job_id"
            if table == TABLES[0]
            else (
                "(attempt_id=NEW.attempt_id AND ordinal=NEW.ordinal) OR "
                "(file_id IS NOT NULL AND file_id=NEW.file_id)"
            )
        )
        op.execute(
            f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE id=NEW.id OR {conflict}) "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: native replacement'); END"
        )
    else:
        op.execute(
            f"CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION immutable_native_service_guard()"
        )
        op.execute(
            f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION immutable_native_service_guard()"
        )


def upgrade() -> None:
    op.add_column(
        "model_services", sa.Column("protocol", sa.String(40), nullable=False, server_default="")
    )
    op.create_table(
        TABLES[0],
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False, unique=True
        ),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column(
            "credential_id", sa.Uuid(), sa.ForeignKey("service_credentials.id"), nullable=False
        ),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("configuration_json", JSON, nullable=False),
        sa.Column("configuration_digest", sa.String(64), nullable=False),
    )
    op.create_table(
        TABLES[1],
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("generation_attempts.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "file_id", sa.Uuid(), sa.ForeignKey("stored_files.id"), nullable=True, unique=True
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("media_json", JSON, nullable=False),
        sa.UniqueConstraint("attempt_id", "ordinal", name="uq_generation_media_output"),
        sa.CheckConstraint("ordinal >= 0", name="media_artifact_ordinal"),
        sa.CheckConstraint(
            "(file_id IS NOT NULL AND error_code IS NULL) OR "
            "(file_id IS NULL AND error_code IS NOT NULL)",
            name="media_artifact_outcome",
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION immutable_native_service_guard() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: native service' "
            "USING ERRCODE='23514'; END; $$"
        )
    for table in TABLES:
        history_guard(table)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Native service downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute(f"LOCK TABLE {', '.join(TABLES)}, model_services IN ACCESS EXCLUSIVE MODE")
    history = any(db.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES)
    if history or db.scalar(sa.text("SELECT 1 FROM model_services WHERE protocol <> '' LIMIT 1")):
        raise RuntimeError("Native service history exists; cannot discard it")
    for table in reversed(TABLES):
        op.drop_table(table)
    if db.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_native_service_guard()")
    op.drop_column("model_services", "protocol")
