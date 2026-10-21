"""initial production foundation"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 初始业务表结构；后续结构变更必须新增迁移版本。
    op.create_table(
        "projects",
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column(
            "style_guide",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "output_profile",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("budget_limit", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("budget_limit >= 0", name=op.f("ck_projects_budget_nonnegative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_projects_owner_id"), ["owner_id"], unique=False)

    op.create_table(
        "episodes",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("episode_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("target_duration_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("episode_no > 0", name=op.f("ck_episodes_episode_positive")),
        sa.CheckConstraint("target_duration_ms > 0", name=op.f("ck_episodes_duration_positive")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_episodes_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episodes")),
        sa.UniqueConstraint("project_id", "episode_no", name=op.f("uq_episodes_project_id")),
    )
    with op.batch_alter_table("episodes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_episodes_project_id"), ["project_id"], unique=False)

    op.create_table(
        "visual_assets",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("asset_type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["current_version_id"],
            ["asset_versions.id"],
            name=op.f("fk_visual_assets_current_version_id_asset_versions"),
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_visual_assets_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_visual_assets")),
    )
    with op.batch_alter_table("visual_assets", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_visual_assets_project_id"), ["project_id"], unique=False
        )

    op.create_table(
        "asset_versions",
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "metadata_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["visual_assets.id"],
            name=op.f("fk_asset_versions_asset_id_visual_assets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_versions")),
        sa.UniqueConstraint("asset_id", "version_no", name=op.f("uq_asset_versions_asset_id")),
    )
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_asset_versions_asset_id"), ["asset_id"], unique=False)

    op.create_table(
        "workflow_runs",
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_type", sa.String(length=64), nullable=False),
        sa.Column("workflow_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "input_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "pending_review",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "resume_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id"], ["episodes.id"], name=op.f("fk_workflow_runs_episode_id_episodes")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_runs")),
        sa.UniqueConstraint(
            "episode_id", "idempotency_key", name=op.f("uq_workflow_runs_episode_id")
        ),
    )
    with op.batch_alter_table("workflow_runs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_workflow_runs_episode_id"), ["episode_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_workflow_runs_status"), ["status"], unique=False)

    op.create_table(
        "asset_dependencies",
        sa.Column("source_asset_version_id", sa.Uuid(), nullable=False),
        sa.Column("target_asset_version_id", sa.Uuid(), nullable=False),
        sa.Column("dependency_type", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_asset_version_id != target_asset_version_id",
            name=op.f("ck_asset_dependencies_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["source_asset_version_id"],
            ["asset_versions.id"],
            name=op.f("fk_asset_dependencies_source_asset_version_id_asset_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["target_asset_version_id"],
            ["asset_versions.id"],
            name=op.f("fk_asset_dependencies_target_asset_version_id_asset_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_dependencies")),
        sa.UniqueConstraint(
            "source_asset_version_id",
            "target_asset_version_id",
            "dependency_type",
            name=op.f("uq_asset_dependencies_source_asset_version_id"),
        ),
    )
    with op.batch_alter_table("asset_dependencies", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_asset_dependencies_source_asset_version_id"),
            ["source_asset_version_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_asset_dependencies_target_asset_version_id"),
            ["target_asset_version_id"],
            unique=False,
        )

    op.create_table(
        "outbox_events",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], name=op.f("fk_outbox_events_run_id_workflow_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
        sa.UniqueConstraint("run_id", "revision", name=op.f("uq_outbox_events_run_id")),
    )
    with op.batch_alter_table("outbox_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_outbox_events_run_id"), ["run_id"], unique=False)
        batch_op.create_index("ix_outbox_unpublished", ["published_at", "created_at"], unique=False)

    op.create_table(
        "reviews",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.String(length=100), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.String(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], name=op.f("fk_reviews_run_id_workflow_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
        sa.UniqueConstraint(
            "run_id", "resource_type", "version_id", name=op.f("uq_reviews_run_id")
        ),
    )
    with op.batch_alter_table("reviews", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_reviews_run_id"), ["run_id"], unique=False)

    op.create_table(
        "scripts",
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "content_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id"], ["episodes.id"], name=op.f("fk_scripts_episode_id_episodes")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], name=op.f("fk_scripts_run_id_workflow_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scripts")),
        sa.UniqueConstraint("episode_id", "version_no", name=op.f("uq_scripts_episode_id")),
        sa.UniqueConstraint("run_id", name=op.f("uq_scripts_run_id")),
    )
    with op.batch_alter_table("scripts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_scripts_episode_id"), ["episode_id"], unique=False)

    op.create_table(
        "workflow_events",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], name=op.f("fk_workflow_events_run_id_workflow_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_events")),
    )
    with op.batch_alter_table("workflow_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_workflow_events_run_id"), ["run_id"], unique=False)

    op.create_table(
        "storyboards",
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "content_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id"], ["episodes.id"], name=op.f("fk_storyboards_episode_id_episodes")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], name=op.f("fk_storyboards_run_id_workflow_runs")
        ),
        sa.ForeignKeyConstraint(
            ["script_version_id"],
            ["scripts.id"],
            name=op.f("fk_storyboards_script_version_id_scripts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_storyboards")),
        sa.UniqueConstraint("episode_id", "version_no", name=op.f("uq_storyboards_episode_id")),
        sa.UniqueConstraint("run_id", name=op.f("uq_storyboards_run_id")),
    )
    with op.batch_alter_table("storyboards", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_storyboards_episode_id"), ["episode_id"], unique=False)

    op.create_table(
        "shots",
        sa.Column("storyboard_id", sa.Uuid(), nullable=False),
        sa.Column("shot_no", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column(
            "content_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("duration_ms > 0", name=op.f("ck_shots_duration_positive")),
        sa.ForeignKeyConstraint(
            ["storyboard_id"], ["storyboards.id"], name=op.f("fk_shots_storyboard_id_storyboards")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shots")),
        sa.UniqueConstraint("storyboard_id", "shot_no", name=op.f("uq_shots_storyboard_id")),
    )
    with op.batch_alter_table("shots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_shots_storyboard_id"), ["storyboard_id"], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_visual_assets_current_version_id_asset_versions",
            "visual_assets",
            "asset_versions",
            ["current_version_id"],
            ["id"],
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(
            "fk_visual_assets_current_version_id_asset_versions",
            "visual_assets",
            type_="foreignkey",
        )
    # 初始业务表结构；后续结构变更必须新增迁移版本。
    with op.batch_alter_table("shots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_shots_storyboard_id"))

    op.drop_table("shots")
    with op.batch_alter_table("storyboards", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_storyboards_episode_id"))

    op.drop_table("storyboards")
    with op.batch_alter_table("workflow_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_workflow_events_run_id"))

    op.drop_table("workflow_events")
    with op.batch_alter_table("scripts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scripts_episode_id"))

    op.drop_table("scripts")
    with op.batch_alter_table("reviews", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_reviews_run_id"))

    op.drop_table("reviews")
    with op.batch_alter_table("outbox_events", schema=None) as batch_op:
        batch_op.drop_index("ix_outbox_unpublished")
        batch_op.drop_index(batch_op.f("ix_outbox_events_run_id"))

    op.drop_table("outbox_events")
    with op.batch_alter_table("asset_dependencies", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_asset_dependencies_target_asset_version_id"))
        batch_op.drop_index(batch_op.f("ix_asset_dependencies_source_asset_version_id"))

    op.drop_table("asset_dependencies")
    with op.batch_alter_table("workflow_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_workflow_runs_status"))
        batch_op.drop_index(batch_op.f("ix_workflow_runs_episode_id"))

    op.drop_table("workflow_runs")
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_asset_versions_asset_id"))

    op.drop_table("asset_versions")
    with op.batch_alter_table("visual_assets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_visual_assets_project_id"))

    op.drop_table("visual_assets")
    with op.batch_alter_table("episodes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_episodes_project_id"))

    op.drop_table("episodes")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projects_owner_id"))

    op.drop_table("projects")
