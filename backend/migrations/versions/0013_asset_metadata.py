"""Asset metadata and attribution without guessing historical authors."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("visual_assets") as batch:
        batch.add_column(sa.Column("metadata_json", JSON, nullable=False, server_default="{}"))
        batch.add_column(sa.Column("creation_source", JSON, nullable=True))
        batch.add_column(sa.Column("created_by", sa.String(100), nullable=True))


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Asset metadata downgrade requires an online preservation check")
    assets = sa.table(
        "visual_assets",
        sa.column("metadata_json", JSON),
        sa.column("creation_source", JSON),
        sa.column("created_by"),
    )
    for row in op.get_bind().execute(sa.select(assets)).mappings():
        if row["metadata_json"] or row["creation_source"] or row["created_by"] is not None:
            raise RuntimeError("Asset metadata exists; back up and migrate before downgrade")
    with op.batch_alter_table("visual_assets") as batch:
        batch.drop_column("created_by")
        batch.drop_column("creation_source")
        batch.drop_column("metadata_json")
