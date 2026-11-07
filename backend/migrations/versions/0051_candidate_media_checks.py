"""Keep deterministic media evidence on the original immutable candidate row."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_candidates",
        sa.Column(
            "media_check_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    # Existing candidates keep NULL: their original checks are not fabricated.
    # The 0036 whole-row guards also protect this new column.


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Media check downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE generation_candidates IN ACCESS EXCLUSIVE MODE")
    else:
        driver = db.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            db.exec_driver_sql("BEGIN IMMEDIATE")
    if db.scalar(
        sa.text("SELECT 1 FROM generation_candidates WHERE media_check_json IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("Generation history contains media checks; cannot discard it")
    op.drop_column("generation_candidates", "media_check_json")
