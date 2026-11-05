"""Transactional production notifications share the existing Outbox dispatcher.

Revision ID: 0048
Revises: 0047
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "production_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id"), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("event_json", JSON, nullable=False),
        sa.UniqueConstraint(
            "source_kind", "source_id", "source_revision", name="uq_production_event_source"
        ),
        sa.CheckConstraint("source_kind IN ('TEXT', 'MEDIA')", name="production_event_kind"),
        sa.CheckConstraint("source_revision >= 0", name="production_event_revision"),
    )
    op.create_index("ix_production_events_project_id", "production_events", ["project_id"])
    op.create_index("ix_production_events_episode_id", "production_events", ["episode_id"])
    op.create_table(
        "production_event_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("production_events.id"), nullable=False),
        sa.Column("evaluation_key", sa.Uuid(), nullable=False),
        sa.Column("result_json", JSON, nullable=False),
        sa.UniqueConstraint("event_id", "evaluation_key"),
    )
    op.create_index(
        "ix_production_event_receipts_event_id", "production_event_receipts", ["event_id"]
    )
    with op.batch_alter_table("outbox_events") as batch:
        batch.alter_column("run_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(sa.Column("production_event_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_outbox_events_production_event_id_production_events",
            "production_events",
            ["production_event_id"],
            ["id"],
        )
        batch.create_unique_constraint(
            "uq_outbox_events_production_event_id", ["production_event_id"]
        )
        batch.create_check_constraint(
            "outbox_target",
            "(run_id IS NOT NULL AND production_event_id IS NULL "
            "AND event_type = 'workflow.execute') OR "
            "(run_id IS NULL AND production_event_id IS NOT NULL "
            "AND event_type = 'production.coordinate')",
        )
    for table in ("production_events", "production_event_receipts"):
        if op.get_bind().dialect.name == "sqlite":
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} "
                    f"ON {table} BEGIN "
                    "SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: production event'); END"
                )
            conflict = (
                " OR (source_kind=NEW.source_kind AND source_id=NEW.source_id "
                "AND source_revision=NEW.source_revision)"
                if table == "production_events"
                else " OR (event_id=NEW.event_id AND evaluation_key=NEW.evaluation_key)"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_replace BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE id=NEW.id{conflict}) "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: production event'); END"
            )
        else:
            op.execute(
                f"CREATE FUNCTION immutable_{table}_guard() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: production event'; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION immutable_{table}_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION immutable_{table}_guard()"
            )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Production event downgrade needs an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE outbox_events, production_events, production_event_receipts "
            "IN ACCESS EXCLUSIVE MODE"
        )
    if db.scalar(sa.text("SELECT 1 FROM production_events LIMIT 1")):
        raise RuntimeError("Production event history exists; cannot discard it")
    with op.batch_alter_table("outbox_events") as batch:
        batch.drop_constraint(op.f("ck_outbox_events_outbox_target"), type_="check")
        batch.drop_constraint("uq_outbox_events_production_event_id", type_="unique")
        batch.drop_constraint(
            "fk_outbox_events_production_event_id_production_events", type_="foreignkey"
        )
        batch.drop_column("production_event_id")
        batch.alter_column("run_id", existing_type=sa.Uuid(), nullable=False)
    for table in ("production_event_receipts", "production_events"):
        op.drop_table(table)
        if db.dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION immutable_{table}_guard()")
