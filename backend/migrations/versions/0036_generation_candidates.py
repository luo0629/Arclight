"""Append-only generation candidates and replacement provenance."""

import sqlalchemy as sa
from alembic import context, op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None
TABLE = "generation_candidates"


def upgrade() -> None:
    op.create_table(
        "generation_image_imports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("generation_attempts.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("output_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("output_count BETWEEN 1 AND 64", name="image_import_count"),
    )
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("generation_attempts.id"), nullable=False),
        sa.Column(
            "input_manifest_id", sa.Uuid(), sa.ForeignKey("dependency_manifests.id"), nullable=False
        ),
        sa.Column("ordinal", sa.Integer()),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("output_handle", sa.String(512)),
        sa.Column("asset_version_id", sa.Uuid(), sa.ForeignKey("asset_versions.id"), unique=True),
        sa.Column("source_file_id", sa.Uuid(), sa.ForeignKey("stored_files.id")),
        sa.Column("replaces_candidate_id", sa.Uuid(), sa.ForeignKey("generation_candidates.id")),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("attempt_id", "ordinal", name="uq_generation_candidate_output"),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_generation_candidate_creation"),
        sa.CheckConstraint(
            "(source='GENERATED' AND ordinal IS NOT NULL AND ordinal >= 0 "
            "AND output_handle IS NOT NULL "
            "AND replaces_candidate_id IS NULL AND source_file_id IS NULL) OR "
            "(source='REPLACEMENT' AND ordinal IS NULL AND output_handle IS NULL "
            "AND replaces_candidate_id IS NOT NULL AND source_file_id IS NOT NULL)",
            name="generation_candidate_source",
        ),
        sa.CheckConstraint(
            "(asset_version_id IS NOT NULL AND error_code IS NULL) OR "
            "(source='GENERATED' AND asset_version_id IS NULL AND error_code IS NOT NULL)",
            name="generation_candidate_outcome",
        ),
    )
    for column in ("project_id", "job_id"):
        op.create_index(f"ix_{TABLE}_{column}", TABLE, [column])
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_{TABLE}_{action.lower()} BEFORE {action} ON {TABLE} "
                f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {TABLE}'); END"
            )
        op.execute(
            f"CREATE TRIGGER immutable_{TABLE}_insert BEFORE INSERT ON {TABLE} "
            f"WHEN EXISTS (SELECT 1 FROM {TABLE} WHERE id=NEW.id OR "
            "(project_id=NEW.project_id AND creation_key=NEW.creation_key) OR "
            "(attempt_id=NEW.attempt_id AND ordinal=NEW.ordinal) OR "
            "asset_version_id=NEW.asset_version_id) "
            f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {TABLE} replacement'); END"
        )
    else:
        op.execute(f"""CREATE FUNCTION immutable_{TABLE}_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: {TABLE}' USING ERRCODE='23514'; END; $$""")
        op.execute(
            f"CREATE TRIGGER immutable_{TABLE}_write BEFORE UPDATE OR DELETE ON {TABLE} "
            f"FOR EACH ROW EXECUTE FUNCTION immutable_{TABLE}_guard()"
        )
        op.execute(
            f"CREATE TRIGGER immutable_{TABLE}_truncate BEFORE TRUNCATE ON {TABLE} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION immutable_{TABLE}_guard()"
        )
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_image_import_{action.lower()} BEFORE {action} "
                "ON generation_image_imports BEGIN SELECT RAISE(ABORT, "
                "'IMMUTABLE_HISTORY: generation_image_imports'); END"
            )
        op.execute(
            "CREATE TRIGGER immutable_image_import_insert BEFORE INSERT "
            "ON generation_image_imports "
            "WHEN EXISTS (SELECT 1 FROM generation_image_imports WHERE id=NEW.id "
            "OR attempt_id=NEW.attempt_id) BEGIN SELECT RAISE(ABORT, "
            "'IMMUTABLE_HISTORY: generation_image_imports replacement'); END"
        )
    else:
        op.execute(
            "CREATE TRIGGER immutable_image_import_write BEFORE UPDATE OR DELETE "
            f"ON generation_image_imports FOR EACH ROW EXECUTE FUNCTION immutable_{TABLE}_guard()"
        )
        op.execute(
            "CREATE TRIGGER immutable_image_import_truncate BEFORE TRUNCATE "
            "ON generation_image_imports FOR EACH STATEMENT "
            f"EXECUTE FUNCTION immutable_{TABLE}_guard()"
        )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Candidate downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(f"LOCK TABLE {TABLE}, generation_image_imports IN ACCESS EXCLUSIVE MODE")
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if connection.scalar(sa.text(f"SELECT 1 FROM {TABLE} LIMIT 1")):
        raise RuntimeError("Generation history exists; cannot discard candidates")
    if connection.scalar(sa.text("SELECT 1 FROM generation_image_imports LIMIT 1")):
        raise RuntimeError("Generation history exists; cannot discard pending image imports")
    op.drop_table(TABLE)
    op.drop_table("generation_image_imports")
    if connection.dialect.name == "postgresql":
        op.execute(f"DROP FUNCTION immutable_{TABLE}_guard()")
