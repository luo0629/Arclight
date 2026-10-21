"""accounts collaboration and canvas"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "audit_events",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column(
            "details_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_audit_events_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_audit_events_project_id"), ["project_id"], unique=False
        )

    op.create_table(
        "auth_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("refresh_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_auth_sessions_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
    )
    with op.batch_alter_table("auth_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_auth_sessions_user_id"), ["user_id"], unique=False)

    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('editor', 'viewer')", name=op.f("ck_project_members_member_role")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_project_members_project_id_projects")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_project_members_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_members")),
        sa.UniqueConstraint("project_id", "user_id", name=op.f("uq_project_members_project_id")),
    )
    with op.batch_alter_table("project_members", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_project_members_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_project_members_user_id"), ["user_id"], unique=False)

    op.create_table(
        "canvas_layouts",
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "positions", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "viewport", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id"], ["episodes.id"], name=op.f("fk_canvas_layouts_episode_id_episodes")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_canvas_layouts")),
        sa.UniqueConstraint("episode_id", name=op.f("uq_canvas_layouts_episode_id")),
    )
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(length=32), server_default="DRAFT", nullable=False)
        )
        batch_op.add_column(
            sa.Column("stale", sa.Boolean(), server_default=sa.false(), nullable=False)
        )

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("archived", sa.Boolean(), server_default=sa.false(), nullable=False)
        )

    with op.batch_alter_table("scripts", schema=None) as batch_op:
        batch_op.alter_column("run_id", existing_type=sa.CHAR(length=32), nullable=True)

    with op.batch_alter_table("storyboards", schema=None) as batch_op:
        batch_op.alter_column("run_id", existing_type=sa.CHAR(length=32), nullable=True)

    # 临时服务端默认值为旧数据回填；应用层仍统一负责新数据默认值。
    with op.batch_alter_table("asset_versions") as batch_op:
        batch_op.alter_column("status", existing_type=sa.String(32), server_default=None)
        batch_op.alter_column("stale", existing_type=sa.Boolean(), server_default=None)
    with op.batch_alter_table("projects") as batch_op:
        batch_op.alter_column("archived", existing_type=sa.Boolean(), server_default=None)


def downgrade() -> None:
    # 0001 要求每个内容版本均有 run；不能为人工版本伪造工作流或静默删数据。
    if not context.is_offline_mode():
        connection = op.get_bind()
        for table in ("scripts", "storyboards"):
            count = connection.scalar(sa.text(f"SELECT COUNT(*) FROM {table} WHERE run_id IS NULL"))
            if count:
                raise RuntimeError(
                    "存在人工内容版本，无法无损降级至 0001；请先备份并制定数据迁移方案"
                )
    with op.batch_alter_table("storyboards", schema=None) as batch_op:
        batch_op.alter_column("run_id", existing_type=sa.CHAR(length=32), nullable=False)

    with op.batch_alter_table("scripts", schema=None) as batch_op:
        batch_op.alter_column("run_id", existing_type=sa.CHAR(length=32), nullable=False)

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("archived")

    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.drop_column("stale")
        batch_op.drop_column("status")

    op.drop_table("canvas_layouts")
    with op.batch_alter_table("project_members", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_project_members_user_id"))
        batch_op.drop_index(batch_op.f("ix_project_members_project_id"))

    op.drop_table("project_members")
    with op.batch_alter_table("auth_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_auth_sessions_user_id"))

    op.drop_table("auth_sessions")
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_events_project_id"))

    op.drop_table("audit_events")
    op.drop_table("users")
