"""Add independent directory revisions without rewriting asset versions or locks."""

import sqlalchemy as sa
from alembic import context, op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("visual_assets") as batch:
        batch.add_column(sa.Column("revision", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("archived_from_status", sa.String(32), nullable=True))
        batch.create_check_constraint("asset_directory_revision", "revision >= 0")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Asset directory downgrade requires an online preservation check")
    assets = sa.table("visual_assets", sa.column("revision"), sa.column("archived_from_status"))
    if (
        op.get_bind().scalar(
            sa.select(assets.c.revision)
            .where(sa.or_(assets.c.revision > 0, assets.c.archived_from_status.is_not(None)))
            .limit(1)
        )
        is not None
    ):
        raise RuntimeError("Asset directory history exists; back up and migrate before downgrade")
    with op.batch_alter_table("visual_assets") as batch:
        batch.drop_constraint("asset_directory_revision", type_="check")
        batch.drop_column("archived_from_status")
        batch.drop_column("revision")
