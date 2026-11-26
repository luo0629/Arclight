"""Account credential migration preserves both active profiles and frozen evidence."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from alembic import command
from cryptography.fernet import Fernet
from sqlalchemy import Column, MetaData, Table, Text, Uuid, cast, inspect, select
from sqlalchemy.engine import Connection
from test_model_services import account
from test_service_credentials import new_key

from arclight.application.model_services import require_text_snapshot
from arclight.application.service_credentials import read_credential, store_credential
from arclight.core.service_keys import keyring
from arclight.persistence.models import ModelService, ServiceCredential, WorkflowRun


def _historical_services(connection: Connection) -> Table:
    # SQLite reflection resolves UUID columns as CHAR. Keep the original bind types.
    return Table(
        "model_services",
        MetaData(),
        Column("id", Uuid(), primary_key=True),
        Column("user_id", Uuid(), nullable=False),
        Column("credential_id", Uuid()),
        autoload_with=connection,
    )


def test_migrate_active_credentials_and_explicit_text_protocol(client, settings, episode, migrated):
    settings.service_encryption_key = new_key()
    user_id = UUID(account(client))
    authorization = client.headers["Authorization"]
    other_user_id = UUID(account(client))
    factory = client.app.state.session_factory
    token = keyring(settings).active.encrypt(b"legacy-secret").decode()
    prior_token = Fernet(Fernet.generate_key()).encrypt(b"prior-secret").decode()
    frozen = {
        "base_url": "https://legacy.example/v1",
        "model": "text-model",
        "encrypted_key": token,
    }
    command.downgrade(migrated, "0073")

    with factory.begin() as db:
        existing = store_credential(db, settings, "current-secret")
        mapped = store_credential(db, settings, "prior-secret")
        mapped.legacy_digest = sha256(prior_token.encode()).hexdigest()
        existing_id, mapped_id, mapped_token = existing.id, mapped.id, mapped.encrypted_key
        run = WorkflowRun(
            episode_id=UUID(episode["id"]),
            trace_id=uuid4().hex,
            idempotency_key=uuid4().hex,
            request_fingerprint="a" * 64,
            input_json={"_text_service": frozen},
        )
        db.add(run)
        db.flush()
        run_id = run.id
        frozen_input = select(cast(WorkflowRun.input_json, Text)).where(WorkflowRun.id == run_id)
        original_input = db.execute(frozen_input).scalar_one()

        services = _historical_services(db.connection())
        rows = [
            (user_id, "text", "", token, None),
            (user_id, "image", "openai_image", token, None),
            (user_id, "speech", "openai_tts", prior_token, existing_id),
            (other_user_id, "text", "gemini", prior_token, None),
        ]
        now = datetime.now(UTC)
        ids = {(owner, kind): uuid4() for owner, kind, _, _, _ in rows}
        db.execute(
            services.insert(),
            [
                dict(
                    id=ids[owner, kind],
                    user_id=owner,
                    kind=kind,
                    protocol=protocol,
                    encrypted_key=ciphertext,
                    credential_id=credential_id,
                    created_at=now,
                    updated_at=now,
                    base_url=frozen["base_url"],
                    model=frozen["model"],
                    supplier="custom",
                    supplier_name="",
                    response_format="json_schema",
                    revision=4,
                )
                for owner, kind, protocol, ciphertext, credential_id in rows
            ],
        )

    command.upgrade(migrated, "head")
    with factory() as db:
        assert "encrypted_key" not in {
            column["name"] for column in inspect(db.connection()).get_columns("model_services")
        }
        text_profile = db.get(ModelService, ids[user_id, "text"])
        image_profile = db.get(ModelService, ids[user_id, "image"])
        speech_profile = db.get(ModelService, ids[user_id, "speech"])
        other_text_profile = db.get(ModelService, ids[other_user_id, "text"])
        assert text_profile.protocol == "openai_chat"
        assert image_profile.protocol == "openai_image"
        assert other_text_profile.protocol == "gemini"
        assert text_profile.revision == image_profile.revision == speech_profile.revision == 4
        assert text_profile.credential_id == image_profile.credential_id
        assert speech_profile.credential_id == existing_id
        assert other_text_profile.credential_id == mapped_id
        assert db.get(ServiceCredential, mapped_id).encrypted_key == mapped_token
        assert read_credential(db, settings, {"encrypted_key": prior_token}) == "prior-secret"
        credential = db.get(ServiceCredential, text_profile.credential_id)
        assert credential.encrypted_key == token
        assert credential.legacy_digest == sha256(token.encode()).hexdigest()
        assert credential.key_id == ""

        assert db.execute(frozen_input).scalar_one() == original_input
        old_snapshot = db.get(WorkflowRun, run_id).input_json["_text_service"]
        assert read_credential(db, settings, old_snapshot) == "legacy-secret"
        current = require_text_snapshot(db, str(user_id), settings)
        assert current["protocol"] == "openai_chat"
        assert current["credential_id"] == str(credential.id)
        assert "encrypted_key" not in current
        assert read_credential(db, settings, current) == "legacy-secret"

    response = client.get("/api/v1/me/model-services", headers={"Authorization": authorization})
    assert response.status_code == 200
    visible = {row["kind"]: row for row in response.json()["services"]}
    assert visible["text"]["key_status"] == "READY"
    assert visible["text"]["protocol"] == "openai_chat"

    command.downgrade(migrated, "0073")
    with factory() as db:
        services = _historical_services(db.connection())
        row = (
            db.execute(select(services).where(services.c.id == ids[user_id, "text"]))
            .mappings()
            .one()
        )
        assert row["encrypted_key"] is None
        assert row["credential_id"] == credential.id and row["protocol"] == "openai_chat"
        assert db.execute(frozen_input).scalar_one() == original_input
        assert read_credential(db, settings, current) == read_credential(db, settings, frozen)
