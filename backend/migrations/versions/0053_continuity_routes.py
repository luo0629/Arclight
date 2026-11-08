"""Allow continuity result notifications on the existing production Outbox."""

import sqlalchemy as sa
from alembic import context, op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def change_kinds(include_review: bool) -> None:
    kinds = "'TEXT', 'MEDIA', 'REVIEW'" if include_review else "'TEXT', 'MEDIA'"
    db = op.get_bind()
    triggers = []
    if db.dialect.name == "sqlite":
        # Batch recreation preserves rows and indexes but drops table triggers.
        # Keep the exact installed guards, including the INSERT OR REPLACE guard.
        triggers = list(
            db.scalars(
                sa.text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='trigger' AND tbl_name='production_events' AND sql IS NOT NULL"
                )
            )
        )
    with op.batch_alter_table("production_events") as batch:
        batch.drop_constraint(op.f("ck_production_events_production_event_kind"), type_="check")
        batch.create_check_constraint("production_event_kind", f"source_kind IN ({kinds})")
    for sql in triggers:
        op.execute(sql)


def upgrade() -> None:
    change_kinds(True)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Continuity route downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE production_events IN ACCESS EXCLUSIVE MODE")
    if db.scalar(sa.text("SELECT 1 FROM production_events WHERE source_kind='REVIEW' LIMIT 1")):
        raise RuntimeError("Cannot discard continuity review routing history")
    change_kinds(False)
