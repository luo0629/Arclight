"""Persist immutable invalidation facts without rewriting historical inputs."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dependency_invalidations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("event_no", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "snapshot_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint(
            "project_id", "creation_key", name="uq_dependency_invalidation_creation"
        ),
        sa.UniqueConstraint("project_id", "event_no", name="uq_dependency_invalidation_event_no"),
    )
    op.create_index(
        "ix_dependency_invalidations_project_id", "dependency_invalidations", ["project_id"]
    )
    if op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(f"""CREATE TRIGGER immutable_dependency_invalidations_{operation.lower()}
                BEFORE {operation} ON dependency_invalidations
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: dependency_invalidations'); END""")
        op.execute("""CREATE TRIGGER immutable_dependency_invalidations_insert
            BEFORE INSERT ON dependency_invalidations WHEN EXISTS (
                SELECT 1 FROM dependency_invalidations AS existing WHERE existing.id = NEW.id
                OR (existing.project_id = NEW.project_id
                    AND (existing.creation_key = NEW.creation_key
                         OR existing.event_no = NEW.event_no)))
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: dependency_invalidations replacement');
            END""")
    else:
        op.execute("""CREATE FUNCTION immutable_dependency_invalidations_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'IMMUTABLE_HISTORY: dependency_invalidations'
                    USING ERRCODE = '23514';
            END; $$""")
        op.execute("""CREATE TRIGGER immutable_dependency_invalidations_write
            BEFORE UPDATE OR DELETE ON dependency_invalidations FOR EACH ROW
            EXECUTE FUNCTION immutable_dependency_invalidations_guard()""")
        op.execute("""CREATE TRIGGER immutable_dependency_invalidations_truncate
            BEFORE TRUNCATE ON dependency_invalidations FOR EACH STATEMENT
            EXECUTE FUNCTION immutable_dependency_invalidations_guard()""")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Invalidation downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        connection.exec_driver_sql("LOCK TABLE dependency_invalidations IN ACCESS EXCLUSIVE MODE")
    if connection.scalar(sa.text("SELECT 1 FROM dependency_invalidations LIMIT 1")):
        raise RuntimeError("Dependency invalidations exist; preserve change history")
    op.drop_table("dependency_invalidations")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_dependency_invalidations_guard()")
