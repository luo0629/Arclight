"""Scene/prop profiles without rewriting historical references."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("visual_assets") as batch:
        batch.add_column(sa.Column("scene_profile", JSON, nullable=True))
        batch.add_column(sa.Column("prop_profile", JSON, nullable=True))


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Scene/prop downgrade requires an online preservation check")
    table = sa.table(
        "visual_assets", sa.column("scene_profile", JSON), sa.column("prop_profile", JSON)
    )
    for scene, prop in op.get_bind().execute(sa.select(table)):
        if scene is not None or prop is not None:
            raise RuntimeError("Scene/prop profiles exist; back up and migrate before downgrade")
    with op.batch_alter_table("visual_assets") as batch:
        batch.drop_column("prop_profile")
        batch.drop_column("scene_profile")
