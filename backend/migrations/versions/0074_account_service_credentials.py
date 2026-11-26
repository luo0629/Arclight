"""Move active account services to registered credentials and explicit text protocols."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    if op.get_context().as_sql:
        raise RuntimeError("Account credential migration requires a live database connection")
    if db.dialect.name == "postgresql":
        # Share the lock order used by credential saves and rotation.
        db.execute(sa.text("SELECT id FROM service_key_state WHERE id = 1 FOR UPDATE")).scalar_one()
        op.execute("LOCK TABLE model_services IN ACCESS EXCLUSIVE MODE")

    services = sa.table(
        "model_services",
        sa.column("id", sa.Uuid()),
        sa.column("kind", sa.String()),
        sa.column("protocol", sa.String()),
        sa.column("credential_id", sa.Uuid()),
        sa.column("encrypted_key", sa.String()),
    )
    credentials = sa.table(
        "service_credentials",
        sa.column("id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("encrypted_key", sa.String()),
        sa.column("key_id", sa.String()),
        sa.column("legacy_digest", sa.String()),
        sa.column("revision", sa.Integer()),
    )
    legacy_services = db.execute(
        sa.select(services.c.id, services.c.encrypted_key, credentials.c.id)
        .select_from(services.outerjoin(credentials, services.c.credential_id == credentials.c.id))
        .where(services.c.encrypted_key != "")
    ).all()
    now = datetime.now(UTC)
    for service_id, token, existing_credential_id in legacy_services:
        digest = sha256(token.encode()).hexdigest()
        credential_id = db.scalar(
            sa.select(credentials.c.id).where(credentials.c.legacy_digest == digest)
        )
        if credential_id is None:
            credential_id = uuid4()
            # The normal key rotation verifies and rewraps these ciphertexts.
            db.execute(
                credentials.insert().values(
                    id=credential_id,
                    created_at=now,
                    updated_at=now,
                    encrypted_key=token,
                    key_id="",
                    legacy_digest=digest,
                    revision=1,
                )
            )
        if existing_credential_id is None:
            db.execute(
                services.update()
                .where(services.c.id == service_id)
                .values(credential_id=credential_id)
            )

    db.execute(
        services.update()
        .where(services.c.kind == "text", services.c.protocol == "")
        .values(protocol="openai_chat")
    )
    # Changes are storage projections of the same service settings, so keep revisions.
    with op.batch_alter_table("model_services") as batch:
        batch.drop_column("encrypted_key")


def downgrade() -> None:
    # Revision 0073 supports registered credentials and explicit protocols. The
    # projected credentials and the original snapshots remain usable after downgrade.
    op.add_column("model_services", sa.Column("encrypted_key", sa.String(8192)))
