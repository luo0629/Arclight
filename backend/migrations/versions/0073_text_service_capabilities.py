"""Record verified capabilities for the current account text service."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_services",
        sa.Column(
            "verified_capabilities", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
        ),
    )
    op.add_column("model_services", sa.Column("capability_probe_id", sa.Uuid()))
    op.add_column("model_services", sa.Column("capability_probe_until", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("model_services", "capability_probe_until")
    op.drop_column("model_services", "capability_probe_id")
    op.drop_column("model_services", "verified_capabilities")
