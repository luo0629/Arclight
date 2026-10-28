"""Exact dependency manifests and immutable output receipts; preserve legacy edges."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

EDGE_FIELDS = (
    "id created_at updated_at source_asset_version_id target_asset_version_id dependency_type"
)
GUARDS = {
    "asset_dependencies": EDGE_FIELDS + " dependency_manifest_id",
    "dependency_manifests": (
        "id created_at updated_at project_id target_asset_id creation_key request_fingerprint "
        "snapshot_json created_by"
    ),
    "dependency_outputs": "id created_at updated_at dependency_manifest_id asset_version_id",
}
UNIQUE = {
    "asset_dependencies": [
        ("source_asset_version_id", "target_asset_version_id", "dependency_type")
    ],
    "dependency_manifests": [("project_id", "creation_key")],
    "dependency_outputs": [("asset_version_id",)],
}


def drop_guard(table: str) -> None:
    label = f"immutable_{table}"
    if op.get_bind().dialect.name == "sqlite":
        for kind in ("update", "delete", "insert"):
            op.execute(f"DROP TRIGGER {label}_{kind}")
    else:
        op.execute(f'DROP TRIGGER {label}_write ON "{table}"')
        op.execute(f'DROP TRIGGER {label}_truncate ON "{table}"')
        op.execute(f"DROP FUNCTION {label}_guard()")


def create_guard(table: str, fields: str) -> None:
    label = f"immutable_{table}"
    columns = fields.split()
    if op.get_bind().dialect.name == "sqlite":
        changed = " OR ".join(f'OLD."{name}" IS NOT NEW."{name}"' for name in columns)
        op.execute(f'''CREATE TRIGGER {label}_update BEFORE UPDATE ON "{table}"
            WHEN {changed} BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END''')
        op.execute(f'''CREATE TRIGGER {label}_delete BEFORE DELETE ON "{table}"
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END''')
        identities = [("id",), *UNIQUE[table]]
        collisions = " OR ".join(
            "("
            + " AND ".join(
                f'NEW."{name}" IS NOT NULL AND existing."{name}" = NEW."{name}"' for name in group
            )
            + ")"
            for group in identities
        )
        op.execute(f'''CREATE TRIGGER {label}_insert BEFORE INSERT ON "{table}"
            WHEN EXISTS (SELECT 1 FROM "{table}" AS existing WHERE {collisions})
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table} replacement'); END''')
    else:
        changed = " OR ".join(f'OLD."{name}" IS DISTINCT FROM NEW."{name}"' for name in columns)
        op.execute(f"""CREATE FUNCTION {label}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
                    RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE = '23514';
                END IF;
                IF {changed} THEN
                    RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END; $$""")
        op.execute(f'''CREATE TRIGGER {label}_write BEFORE UPDATE OR DELETE ON "{table}"
            FOR EACH ROW EXECUTE FUNCTION {label}_guard()''')
        op.execute(f'''CREATE TRIGGER {label}_truncate BEFORE TRUNCATE ON "{table}"
            FOR EACH STATEMENT EXECUTE FUNCTION {label}_guard()''')


def upgrade() -> None:
    connection = op.get_bind()
    if (
        connection.dialect.name == "sqlite"
        and not connection.connection.driver_connection.in_transaction
    ):
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "dependency_manifests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("target_asset_id", sa.Uuid(), sa.ForeignKey("visual_assets.id"), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("snapshot_json", json_type, nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_dependency_manifest_creation"),
    )
    for column in ("project_id", "target_asset_id"):
        op.create_index(f"ix_dependency_manifests_{column}", "dependency_manifests", [column])
    op.create_table(
        "dependency_outputs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "dependency_manifest_id",
            sa.Uuid(),
            sa.ForeignKey("dependency_manifests.id"),
            nullable=False,
        ),
        sa.Column(
            "asset_version_id", sa.Uuid(), sa.ForeignKey("asset_versions.id"), nullable=False
        ),
        sa.UniqueConstraint("asset_version_id"),
    )
    op.create_index(
        "ix_dependency_outputs_dependency_manifest_id",
        "dependency_outputs",
        ["dependency_manifest_id"],
    )
    drop_guard("asset_dependencies")
    with op.batch_alter_table("asset_dependencies") as batch:
        batch.add_column(sa.Column("dependency_manifest_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_dependency_manifest", "dependency_manifests", ["dependency_manifest_id"], ["id"]
        )
        batch.create_index(
            "ix_asset_dependencies_dependency_manifest_id", ["dependency_manifest_id"]
        )
    for table, fields in GUARDS.items():
        create_guard(table, fields)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Dependency downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        connection.exec_driver_sql(
            "LOCK TABLE dependency_manifests, dependency_outputs, asset_dependencies "
            "IN ACCESS EXCLUSIVE MODE"
        )
    if connection.scalar(sa.text("SELECT 1 FROM dependency_manifests LIMIT 1")):
        raise RuntimeError("Dependency manifests exist; preserve immutable input history")
    for table in GUARDS:
        drop_guard(table)
    op.drop_table("dependency_outputs")
    with op.batch_alter_table("asset_dependencies") as batch:
        batch.drop_index("ix_asset_dependencies_dependency_manifest_id")
        batch.drop_constraint("fk_dependency_manifest", type_="foreignkey")
        batch.drop_column("dependency_manifest_id")
    op.drop_table("dependency_manifests")
    create_guard("asset_dependencies", EDGE_FIELDS)
