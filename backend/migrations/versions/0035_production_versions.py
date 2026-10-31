"""Immutable production component versions and execution/output bindings."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None
TABLES = ("production_versions", "version_bindings")


def entity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "production_versions",
        *entity(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("family", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("version", sa.String(160), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("definition_json", json_type, nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "family",
            "kind",
            "name",
            "version",
            "digest",
            name="uq_production_version",
        ),
        sa.CheckConstraint("family IN ('TEXT','ART','REVIEW','MEDIA')", name="version_family"),
        sa.CheckConstraint("kind IN ('AGENT','PROMPT','SCHEMA','MODEL')", name="version_kind"),
    )
    op.create_index("ix_production_versions_project_id", "production_versions", ["project_id"])
    op.create_table(
        "version_bindings",
        *entity(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_json", json_type, nullable=False),
        sa.UniqueConstraint(
            "project_id", "resource_type", "resource_id", name="uq_version_binding"
        ),
    )
    op.create_index("ix_version_bindings_project_id", "version_bindings", ["project_id"])
    op.create_index("ix_version_bindings_resource_id", "version_bindings", ["resource_id"])
    for table in TABLES:
        name = "immutable_" + table
        if op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {name}_{action.lower()} BEFORE {action} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END"
                )
            duplicate = "id = NEW.id"
            if table == "production_versions":
                duplicate += (
                    " OR (project_id=NEW.project_id AND family=NEW.family AND "
                    "kind=NEW.kind AND name=NEW.name AND version=NEW.version "
                    "AND digest=NEW.digest)"
                )
            else:
                duplicate += (
                    " OR (project_id=NEW.project_id AND resource_type=NEW.resource_type "
                    "AND resource_id=NEW.resource_id)"
                )
            op.execute(
                f"CREATE TRIGGER {name}_insert BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {duplicate}) "
                f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table} replacement'); END"
            )
        else:
            op.execute(f"""CREATE FUNCTION {name}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE='23514';
                END; $$""")
            op.execute(
                f"CREATE TRIGGER {name}_write BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {name}_guard()"
            )
            op.execute(
                f"CREATE TRIGGER {name}_truncate BEFORE TRUNCATE ON {table} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION {name}_guard()"
            )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Version history downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(f"LOCK TABLE {', '.join(TABLES)} IN ACCESS EXCLUSIVE MODE")
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if any(connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        media = connection.scalar(
            sa.text("SELECT 1 FROM version_bindings WHERE resource_type='GENERATION' LIMIT 1")
        )
        prefix = "Generation history exists; " if media else ""
        raise RuntimeError(prefix + "Production version history exists; cannot discard provenance")
    for table in reversed(TABLES):
        op.drop_table(table)
        if connection.dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION immutable_{table}_guard()")
