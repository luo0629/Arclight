"""Typed character profile, preserving legacy metadata and media."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("visual_assets") as batch:
        batch.add_column(sa.Column("character_profile", JSON, nullable=True))


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Character profile downgrade requires an online preservation check")
    table = sa.table("visual_assets", sa.column("character_profile", JSON))
    if any(
        value is not None for value in op.get_bind().scalars(sa.select(table.c.character_profile))
    ):
        raise RuntimeError("Character profiles exist; back up and migrate before downgrade")
    with op.batch_alter_table("visual_assets") as batch:
        batch.drop_column("character_profile")
