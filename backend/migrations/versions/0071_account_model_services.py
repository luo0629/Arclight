"""Retire the server mode for personal service profiles."""

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Inactive profiles must not activate an old credential after removing mode.
    # Credential records, audit entries and frozen task inputs are independent.
    op.execute(
        "UPDATE model_services SET supplier = 'custom', supplier_name = '', "
        "base_url = '', model = '', protocol = '', response_format = 'json_schema', "
        "credential_id = NULL, encrypted_key = NULL WHERE mode = 'server'"
    )
    with op.batch_alter_table("model_services") as batch:
        batch.drop_column("mode")


def downgrade() -> None:
    with op.batch_alter_table("model_services") as batch:
        batch.add_column(sa.Column("mode", sa.String(16), nullable=True))
    op.execute(
        "UPDATE model_services SET mode = CASE "
        "WHEN credential_id IS NOT NULL OR encrypted_key IS NOT NULL THEN 'custom' "
        "ELSE 'server' END"
    )
    with op.batch_alter_table("model_services") as batch:
        batch.alter_column("mode", existing_type=sa.String(16), nullable=False)
