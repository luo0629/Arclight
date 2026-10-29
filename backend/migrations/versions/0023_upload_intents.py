"""Server-owned, expiring upload capabilities; never client-selected paths."""

import sqlalchemy as sa
from alembic import context, op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "upload_intents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("bucket", sa.String(63), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False, unique=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("original_name", sa.String(160), nullable=False),
        sa.Column("declared_media_type", sa.String(100), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.CheckConstraint("byte_size > 0", name="upload_intent_size"),
    )
    op.create_index("ix_upload_intents_project_id", "upload_intents", ["project_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Upload intent downgrade requires online data check")
    if op.get_bind().execute(sa.text("SELECT 1 FROM upload_intents LIMIT 1")).first():
        raise RuntimeError("Cannot remove saved upload intents")
    op.drop_table("upload_intents")
