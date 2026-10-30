"""Generic media jobs and independently identifiable provider attempts."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

STATES = (
    "'CREATED', 'SUBMITTING', 'SUBMITTED', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', "
    "'FAILED', 'CANCEL_REQUESTED', 'CANCELLED', 'RESULT_UNKNOWN', 'RECONCILING', 'MANUAL_REVIEW'"
)


def common():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "generation_jobs",
        *common(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id")),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_asset_id", sa.Uuid(), sa.ForeignKey("visual_assets.id")),
        sa.Column(
            "input_manifest_id", sa.Uuid(), sa.ForeignKey("dependency_manifests.id"), nullable=False
        ),
        sa.Column("task_type", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("provider_request_id", sa.String(200)),
        sa.Column("expected_asset_revision", sa.Integer()),
        sa.Column("expected_lock_version", sa.Integer()),
        sa.Column("expected_current_version_id", sa.Uuid(), sa.ForeignKey("asset_versions.id")),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_generation_job_idempotency"),
        sa.CheckConstraint(f"status IN ({STATES})", name="generation_job_status"),
        sa.CheckConstraint("revision >= 0 AND attempt_count >= 0", name="generation_job_counters"),
        sa.CheckConstraint(
            "target_type IN ('CHARACTER', 'SCENE', 'PROP', 'STYLE', 'SHOT_IMAGE', "
            "'AUDIO', 'VIDEO', 'RENDER')",
            name="generation_target_type",
        ),
        sa.CheckConstraint(
            "task_type IN ('IMAGE', 'AUDIO', 'VIDEO', 'RENDER')", name="generation_task_type"
        ),
        sa.CheckConstraint(
            "(expected_asset_revision IS NULL OR expected_asset_revision >= 0) AND "
            "(expected_lock_version IS NULL OR expected_lock_version >= 0)",
            name="generation_expected_revisions",
        ),
    )
    for column in ("project_id", "episode_id", "target_asset_id", "input_manifest_id"):
        op.create_index(f"ix_generation_jobs_{column}", "generation_jobs", [column])
    op.create_index(
        "ix_generation_jobs_status_created", "generation_jobs", ["status", "created_at"]
    )
    op.create_table(
        "generation_attempts",
        *common(),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "input_manifest_id", sa.Uuid(), sa.ForeignKey("dependency_manifests.id"), nullable=False
        ),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("provider_request_id", sa.String(200)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_snapshot_json", json_type, nullable=False),
        sa.Column("result_json", json_type),
        sa.Column("error_code", sa.String(64)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("cost", sa.Numeric(18, 6)),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("job_id", "attempt_no", name="uq_generation_attempt_no"),
        sa.UniqueConstraint(
            "provider", "provider_request_id", name="uq_generation_provider_request"
        ),
        sa.CheckConstraint("attempt_no > 0", name="generation_attempt_positive"),
        sa.CheckConstraint(f"status IN ({STATES})", name="generation_attempt_status"),
        sa.CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="generation_latency"),
        sa.CheckConstraint("cost IS NULL OR cost >= 0", name="generation_cost"),
    )
    for column in ("job_id", "input_manifest_id"):
        op.create_index(f"ix_generation_attempts_{column}", "generation_attempts", [column])
    op.create_index(
        "ix_generation_attempts_status_created", "generation_attempts", ["status", "created_at"]
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Generation downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        connection.exec_driver_sql(
            "LOCK TABLE generation_jobs, generation_attempts IN ACCESS EXCLUSIVE MODE"
        )
    for table in ("generation_jobs", "generation_attempts"):
        if connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")):
            raise RuntimeError("Generation history exists; preserve jobs and attempts")
    op.drop_table("generation_attempts")
    op.drop_table("generation_jobs")
