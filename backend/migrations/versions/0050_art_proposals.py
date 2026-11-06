"""Version manual art proposals, review selections and formal design approvals."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TABLES = ("art_proposals", "art_proposal_versions", "art_proposal_reviews", "art_approved_versions")
CONFLICTS = {
    TABLES[1]: " OR (proposal_id=NEW.proposal_id AND "
    "(version_no=NEW.version_no OR creation_key=NEW.creation_key))",
    TABLES[2]: " OR version_id=NEW.version_id",
    TABLES[3]: " OR (proposal_version_id=NEW.proposal_version_id AND design_id=NEW.design_id)"
    " OR (asset_id=NEW.asset_id AND version_no=NEW.version_no)",
}


def identity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        TABLES[0],
        *identity(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("project_id", "creation_key"),
        sa.CheckConstraint("revision >= 0 AND version_no > 0", name="art_proposal_revision"),
    )
    op.create_index("ix_art_proposals_project_id", TABLES[0], ["project_id"])
    op.create_table(
        TABLES[1],
        *identity(),
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("art_proposals.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("proposal_json", JSON, nullable=False),
        sa.Column("comparison_json", JSON, nullable=False),
        sa.Column("baseline_json", JSON, nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("proposal_id", "version_no", name="uq_art_proposal_version_number"),
        sa.UniqueConstraint("proposal_id", "creation_key", name="uq_art_proposal_version_creation"),
        sa.CheckConstraint("version_no > 0", name="art_proposal_version_positive"),
    )
    op.create_index("ix_art_proposal_versions_proposal_id", TABLES[1], ["proposal_id"])
    op.create_table(
        TABLES[2],
        *identity(),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey(TABLES[1] + ".id"), nullable=False),
        sa.Column("confirmation_json", JSON, nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("version_id"),
    )
    op.create_table(
        TABLES[3],
        *identity(),
        sa.Column(
            "proposal_version_id", sa.Uuid(), sa.ForeignKey(TABLES[1] + ".id"), nullable=False
        ),
        sa.Column("design_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("visual_assets.id")),
        sa.Column("visual_bible_version_id", sa.Uuid(), sa.ForeignKey("visual_bible_versions.id")),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("before_json", JSON, nullable=True),
        sa.Column("content_json", JSON, nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("proposal_version_id", "design_id"),
        sa.UniqueConstraint("asset_id", "version_no"),
        sa.CheckConstraint(
            "(asset_id IS NULL) <> (visual_bible_version_id IS NULL)", name="art_output_target"
        ),
    )
    op.create_index("ix_art_approved_versions_asset_id", TABLES[3], ["asset_id"])
    for table, conflict in CONFLICTS.items():
        if op.get_bind().dialect.name == "sqlite":
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} "
                    f"ON {table} BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: art proposal'); END"
                )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_replace BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE id=NEW.id{conflict}) "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: art proposal'); END"
            )
        else:
            op.execute(
                f"CREATE FUNCTION immutable_{table}_guard() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: art proposal'; END; $$"
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
        raise RuntimeError("Art proposal downgrade needs an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE " + ", ".join(TABLES) + " IN ACCESS EXCLUSIVE MODE")
    if any(db.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        raise RuntimeError("Art proposal history exists; cannot discard it")
    for table in reversed(TABLES):
        op.drop_table(table)
        if db.dialect.name == "postgresql" and table in CONFLICTS:
            op.execute(f"DROP FUNCTION immutable_{table}_guard()")
