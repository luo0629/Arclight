"""Explicit asset selection locks; retain prior directory state."""

import sqlalchemy as sa
from alembic import context, op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_selection_locks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("visual_assets.id"), nullable=False),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("asset_versions.id"), nullable=False),
        sa.Column("previous_status", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("asset_id"),
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Selection lock downgrade requires an online preservation check")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM asset_selection_locks")):
        raise RuntimeError("Active selection locks exist; unlock explicitly before downgrade")
    op.drop_table("asset_selection_locks")
