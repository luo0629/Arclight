"""Asset version creation identity and lifecycle CAS, preserving old objects."""

import sqlalchemy as sa
from alembic import context, op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("asset_versions") as batch:
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("creation_key", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("archived_from_status", sa.String(32), nullable=True))
        batch.create_unique_constraint("uq_asset_version_creation", ["asset_id", "creation_key"])
        batch.create_check_constraint("asset_version_revision", "revision >= 0")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Asset version downgrade requires an online preservation check")
    table = sa.table(
        "asset_versions",
        sa.column("revision", sa.Integer()),
        sa.column("creation_key", sa.Uuid()),
        sa.column("archived_from_status", sa.String(32)),
    )
    changed = op.get_bind().scalar(
        sa.select(sa.func.count())
        .select_from(table)
        .where(
            (table.c.revision != 0)
            | table.c.creation_key.is_not(None)
            | table.c.archived_from_status.is_not(None)
        )
    )
    if changed:
        raise RuntimeError("Asset version lifecycle data exist; preserve them before downgrade")
    with op.batch_alter_table("asset_versions") as batch:
        batch.drop_constraint("uq_asset_version_creation", type_="unique")
        batch.drop_constraint(op.f("ck_asset_versions_asset_version_revision"), type_="check")
        batch.drop_column("archived_from_status")
        batch.drop_column("creation_key")
        batch.drop_column("revision")
