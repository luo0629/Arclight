"""Use current capabilities for new text snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_services") as batch:
        batch.drop_column("response_format")


def downgrade() -> None:
    op.add_column(
        "model_services",
        sa.Column("response_format", sa.String(20), nullable=False, server_default="json_schema"),
    )
    with op.batch_alter_table("model_services") as batch:
        batch.alter_column("response_format", server_default=None)
