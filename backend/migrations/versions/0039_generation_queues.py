"""Durable queue classification and fenced dispatch permits.

Revision ID: 0039
Revises: 0038
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

HISTORY = {
    "generation_lanes": "id=NEW.id OR job_id=NEW.job_id",
    "generation_permit_events": "id=NEW.id OR (permit_id=NEW.permit_id AND sequence=NEW.sequence)",
}


def entity() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "generation_lanes",
        *entity(),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("lane", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.UniqueConstraint("job_id"),
        sa.CheckConstraint("lane IN ('INTERACTIVE','BATCH')", name="generation_lane"),
        sa.CheckConstraint("policy_version = 1", name="generation_lane_version"),
    )
    op.create_table(
        "generation_queue_state",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("interactive_cursor", sa.String(200), nullable=False),
        sa.Column("batch_cursor", sa.String(200), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("recovery_publish_after", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id = 'global'", name="generation_queue_singleton"),
    )
    op.execute(
        "INSERT INTO generation_queue_state (id, turn, interactive_cursor, batch_cursor) "
        "VALUES ('global', 0, '', '')"
    )
    op.create_table(
        "generation_permits",
        *entity(),
        sa.Column(
            "dispatch_id", sa.Uuid(), sa.ForeignKey("generation_dispatches.id"), nullable=False
        ),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("lane", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("publish_after", sa.DateTime(timezone=True)),
        sa.Column("publish_count", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.UniqueConstraint("dispatch_id"),
        sa.CheckConstraint("lane IN ('INTERACTIVE','BATCH')", name="generation_permit_lane"),
        sa.CheckConstraint(
            "state IN ('WAITING','ISSUED','CONSUMED','REVOKED')", name="generation_permit_state"
        ),
        sa.CheckConstraint("epoch >= 0 AND publish_count >= 0", name="generation_permit_counters"),
        sa.CheckConstraint(
            "state != 'ISSUED' OR (issued_at IS NOT NULL AND expires_at IS NOT NULL "
            "AND publish_after IS NOT NULL AND epoch > 0)",
            name="generation_permit_issued",
        ),
    )
    op.create_index("ix_generation_permits_job_id", "generation_permits", ["job_id"])
    op.create_index(
        "ix_generation_permits_capacity", "generation_permits", ["state", "expires_at", "provider"]
    )
    op.create_table(
        "generation_permit_events",
        *entity(),
        sa.Column("permit_id", sa.Uuid(), sa.ForeignKey("generation_permits.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.UniqueConstraint("permit_id", "sequence"),
    )
    op.create_index(
        "ix_generation_permit_events_permit_id", "generation_permit_events", ["permit_id"]
    )
    # Stop old dispatchers/consumers before upgrading. Historical origin cannot be inferred
    # from a missing bulk flag: retain input history and classify conservatively as BATCH.
    bind = op.get_bind()
    identity = (
        "gen_random_uuid()" if bind.dialect.name == "postgresql" else "lower(hex(randomblob(16)))"
    )
    op.execute(
        f"INSERT INTO generation_lanes (id, created_at, updated_at, job_id, lane, origin, "
        f"policy_version, actor) SELECT {identity}, created_at, updated_at, id, 'BATCH', "
        "'LEGACY_UNCLASSIFIED', 1, created_by FROM generation_jobs"
    )
    op.execute(
        "INSERT INTO generation_permits (id, created_at, updated_at, dispatch_id, job_id, "
        "project_id, provider, actor, lane, state, epoch, publish_count) "
        f"SELECT {identity}, d.created_at, d.updated_at, d.id, j.id, j.project_id, j.provider, "
        "j.created_by, 'BATCH', CASE WHEN d.status='READY' THEN 'WAITING' ELSE 'REVOKED' END, "
        "0, 0 FROM generation_dispatches d JOIN generation_jobs j ON j.id=d.job_id"
    )
    if bind.dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION immutable_generation_queue_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'IMMUTABLE_HISTORY: generation queue' USING ERRCODE='23514';
            END; $$""")
    for table, duplicate in HISTORY.items():
        if bind.dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END"
                )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {duplicate}) "
                f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table} replacement'); END"
            )
        else:
            op.execute(
                f"CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION immutable_generation_queue_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION immutable_generation_queue_guard()"
            )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Queue downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE generation_lanes, generation_permits, generation_permit_events "
            "IN ACCESS EXCLUSIVE MODE"
        )
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if connection.scalar(sa.text("SELECT 1 FROM generation_lanes LIMIT 1")):
        raise RuntimeError("Generation history exists; cannot discard queue scheduling identities")
    op.drop_table("generation_permit_events")
    op.drop_table("generation_permits")
    op.drop_table("generation_queue_state")
    op.drop_table("generation_lanes")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_generation_queue_guard()")
