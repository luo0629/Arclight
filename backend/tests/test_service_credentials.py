"""SVC-03: isolated key injection, immutable snapshots, atomic rotation and recovery."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_model_services import account, profile

from arclight.application.model_services import run_settings, text_snapshot
from arclight.application.service_credentials import (
    key_state,
    read_credential,
    rotate_credentials,
    store_credential,
    verify_credentials,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.core.service_keys import keyring
from arclight.persistence.models import (
    ModelService,
    ServiceAudit,
    ServiceCredential,
    ServiceKeyState,
    WorkflowRun,
)
from arclight.persistence.session import build_engine, session_factory


def new_key():
    return SecretStr(Fernet.generate_key().decode())


def test_environment_and_secret_file_injection(monkeypatch, tmp_path):
    active, previous = new_key(), new_key()
    monkeypatch.setenv("ARCLIGHT_SERVICE_ENCRYPTION_KEY", active.get_secret_value())
    monkeypatch.setenv(
        "ARCLIGHT_SERVICE_ENCRYPTION_PREVIOUS_KEYS", json.dumps([previous.get_secret_value()])
    )
    direct = Settings(_env_file=None)
    token = Fernet(previous.get_secret_value()).encrypt(b"test-secret").decode()
    assert keyring(direct).decrypt(token) == "test-secret"
    active_file, previous_file = tmp_path / "active", tmp_path / "previous.json"
    active_file.write_text(active.get_secret_value() + "\n", encoding="utf-8")
    previous_file.write_text(json.dumps([previous.get_secret_value()]), encoding="utf-8")
    monkeypatch.setenv("ARCLIGHT_SERVICE_ENCRYPTION_KEY", "")
    monkeypatch.setenv("ARCLIGHT_SERVICE_ENCRYPTION_PREVIOUS_KEYS", "[]")
    monkeypatch.setenv("ARCLIGHT_SERVICE_ENCRYPTION_KEY_FILE", str(active_file))
    monkeypatch.setenv("ARCLIGHT_SERVICE_ENCRYPTION_PREVIOUS_KEYS_FILE", str(previous_file))
    mounted = Settings(_env_file=None)
    assert keyring(mounted).decrypt(token) == "test-secret"
    assert keyring(mounted).key_id == keyring(direct).key_id
    assert active.get_secret_value() not in repr(mounted)
    for invalid in (
        {"service_encryption_key": active},
        {"service_encryption_previous_keys": [previous]},
        {"service_encryption_key_file": tmp_path / "missing"},
    ):
        with pytest.raises(DomainError, match="配置无效"):
            keyring(mounted.model_copy(update=invalid))
    previous_file.write_text('{"bad": "not-an-array"}', encoding="utf-8")
    with pytest.raises(DomainError, match="配置无效"):
        keyring(mounted)


@pytest.mark.parametrize("value", ["", "invalid-sensitive-key"])
def test_production_rejects_missing_or_invalid_key_without_echo(value):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, environment="production", service_encryption_key=value)
    assert "服务" in str(caught.value)
    if value:
        assert value not in str(caught.value)


def test_rotation_preserves_current_and_legacy_task_snapshots(client, settings, episode):
    settings.service_encryption_key = new_key()
    uid = account(client)
    assert (
        client.put(
            "/api/v1/me/model-services/text", json=profile(mode="custom", api_key="current-secret")
        ).status_code
        == 200
    )
    factory = client.app.state.session_factory
    token = keyring(settings).active.encrypt(b"legacy-secret").decode()
    legacy_snapshot = {
        "base_url": "https://legacy.example/v1",
        "model": "old-model",
        "encrypted_key": token,
    }
    with factory.begin() as db:
        frozen = text_snapshot(db, uid)
        run = WorkflowRun(
            episode_id=UUID(episode["id"]),
            trace_id=uuid4().hex,
            idempotency_key=uuid4().hex,
            request_fingerprint="a" * 64,
            input_json={"_text_service": legacy_snapshot},
        )
        db.add(run)
        db.flush()
        run_id, original_input = run.id, json.dumps(run.input_json, sort_keys=True)
        # Simulate an existing 0003 profile without mutating any workflow input.
        db.add(ModelService(user_id=UUID(uid), kind="image", encrypted_key=token))
    rotated = settings.model_copy(
        update={
            "service_encryption_key": new_key(),
            "service_encryption_previous_keys": [settings.service_encryption_key],
        }
    )
    with factory.begin() as db:
        report = rotate_credentials(db, rotated, "rotation-test")
    assert report["rewrapped"] == 1 and report["migrated_profiles"] == 1
    retired = rotated.model_copy(update={"service_encryption_previous_keys": []})
    with factory() as db:
        assert json.dumps(db.get(WorkflowRun, run_id).input_json, sort_keys=True) == original_input
        assert run_settings(retired, frozen, db).llm_api_key.get_secret_value() == "current-secret"
        assert (
            run_settings(retired, legacy_snapshot, db).llm_api_key.get_secret_value()
            == "legacy-secret"
        )
        assert verify_credentials(db, retired)["legacy_tokens"] == 1
        migrated = db.scalar(select(ModelService).where(ModelService.kind == "image"))
        assert migrated.encrypted_key is None and migrated.credential_id
        audit = db.scalar(select(ServiceAudit).where(ServiceAudit.action == "key.rotated"))
        assert audit.actor == "rotation-test" and "secret" not in json.dumps(audit.details_json)
    with factory.begin() as db, pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"):
        db.execute(update(WorkflowRun).where(WorkflowRun.id == run_id).values(input_json={}))
    # A process still configured with the old write key must not write or preserve keys.
    for patch in ({"api_key": "stale-process-secret"}, {}):
        rejected = client.put(
            "/api/v1/me/model-services/text", json=profile(expected_revision=1, **patch)
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "KEY_STORAGE_STALE"


def test_failed_rotation_rolls_back_every_change(client, settings):
    settings.service_encryption_key = new_key()
    factory = client.app.state.session_factory
    with factory.begin() as db:
        valid = store_credential(db, settings, "valid-secret")
        original_id, original_token = valid.id, valid.encrypted_key
        db.add(ServiceCredential(encrypted_key="corrupt-sensitive-ciphertext", key_id="unknown"))
    old_id = keyring(settings).key_id
    rotated = settings.model_copy(
        update={
            "service_encryption_key": new_key(),
            "service_encryption_previous_keys": [settings.service_encryption_key],
        }
    )
    with pytest.raises(DomainError, match="无法解密"), factory.begin() as db:
        rotate_credentials(db, rotated, "failure-test")
    with factory() as db:
        assert db.get(ServiceCredential, original_id).encrypted_key == original_token
        assert db.get(ServiceKeyState, 1).active_key_id == old_id
        assert db.scalar(select(func.count()).select_from(ServiceAudit)) == 0


def test_rotation_serializes_with_concurrent_key_writer(client, settings):
    settings.service_encryption_key = new_key()
    factory = client.app.state.session_factory
    with factory.begin() as db:
        store_credential(db, settings, "before")
    rotated = settings.model_copy(
        update={
            "service_encryption_key": new_key(),
            "service_encryption_previous_keys": [settings.service_encryption_key],
        }
    )
    acquired, release, writer_started, writer_finished = Event(), Event(), Event(), Event()

    def rotate():
        with factory.begin() as db:
            key_state(db)
            acquired.set()
            assert release.wait(5)
            rotate_credentials(db, rotated, "concurrency-test")

    def write():
        writer_started.set()
        try:
            with factory.begin() as db:
                store_credential(db, settings, "must-not-write")
        except DomainError as exc:
            return exc.code
        finally:
            writer_finished.set()
        return "unexpected-success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        rotation = executor.submit(rotate)
        assert acquired.wait(5)
        writer = executor.submit(write)
        assert writer_started.wait(5)
        try:
            assert not writer_finished.wait(0.1)
        finally:
            release.set()
        rotation.result(timeout=10)
        assert writer.result(timeout=10) == "KEY_STORAGE_STALE"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ServiceCredential)) == 1


def test_service_audit_cannot_be_changed_or_removed(client):
    factory = client.app.state.session_factory
    with factory.begin() as db:
        row = ServiceAudit(actor="test", action="key.rotated", details_json={})
        db.add(row)
        db.flush()
        identity = row.id
    statements = ["UPDATE service_audit SET actor='changed'", "DELETE FROM service_audit"]
    if client.app.state.engine.dialect.name == "postgresql":
        statements.append("TRUNCATE service_audit")
    else:
        statements.append("INSERT OR REPLACE INTO service_audit SELECT * FROM service_audit")
    for statement in statements:
        with factory.begin() as db, pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"):
            db.execute(text(statement))
    with factory() as db:
        assert db.get(ServiceAudit, identity).actor == "test"


def test_secret_file_and_database_backup_restore_drill(tmp_path):
    # A wholly separate SQLite database is intentional, even on the PostgreSQL CI job.
    # It exercises the actual SQLite backup API and secret loss, not exported plaintext.
    active_file, backup_file = tmp_path / "active.key", tmp_path / "backup.key"
    active_file.write_text(new_key().get_secret_value(), encoding="utf-8")
    backup_file.write_bytes(active_file.read_bytes())
    database, backup = tmp_path / "live.db", tmp_path / "backup.db"
    configuration = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite:///{database.as_posix()}",
        service_encryption_key_file=active_file,
    )
    migration = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    migration.attributes["database_url"] = configuration.database_url
    command.upgrade(migration, "head")
    engine = build_engine(configuration)
    with session_factory(engine).begin() as db:
        identity = store_credential(db, configuration, "restored-secret").id
    engine.dispose()
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    active_file.unlink()
    with pytest.raises(DomainError, match="配置无效"):
        keyring(configuration)
    active_file.write_bytes(backup_file.read_bytes())
    restored = configuration.model_copy(update={"database_url": f"sqlite:///{backup.as_posix()}"})
    engine = build_engine(restored)
    try:
        with session_factory(engine)() as db:
            assert (
                read_credential(db, restored, {"credential_id": str(identity)}) == "restored-secret"
            )
            assert verify_credentials(db, restored)["credentials"] == 1
    finally:
        engine.dispose()


def test_operator_cli_never_prints_driver_parameters(monkeypatch, capsys):
    from arclight.workers import service_keys

    monkeypatch.setattr("sys.argv", ["service_keys", "check"])
    monkeypatch.setattr(service_keys, "get_settings", lambda: Settings(_env_file=None))

    def fail(_settings):
        raise DBAPIError("SQL", {"ciphertext": "must-remain-secret"}, Exception("sensitive"))

    monkeypatch.setattr(service_keys, "build_engine", fail)
    assert service_keys.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output)["code"] == "KEY_STORAGE_CHECK_FAILED"
    assert "sensitive" not in output and "must-remain-secret" not in output
