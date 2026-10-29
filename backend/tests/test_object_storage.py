import hashlib
import os
from uuid import uuid4

import pytest
from pydantic import ValidationError

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.storage.objects import ObjectStorage, initialize, safe_key


@pytest.fixture(params=["local", "s3"])
def storage(request, tmp_path):
    if request.param == "s3":
        endpoint = os.getenv("ARCLIGHT_TEST_S3_ENDPOINT")
        if not endpoint:
            pytest.skip("Requires isolated S3 test service")
        settings = Settings(
            _env_file=None,
            environment="test",
            object_storage_backend="s3",
            s3_endpoint_url=endpoint,
            s3_bucket="arclight-test-" + uuid4().hex,
            s3_access_key="arclight-test",
            s3_secret_key="arclight-test-secret",
        )
    else:
        settings = Settings(_env_file=None, asset_storage_path=tmp_path / "objects")
    initialize(settings)
    store = ObjectStorage(settings)
    yield store
    if store.client is not None:
        contents = store.client.list_objects_v2(Bucket=settings.s3_bucket).get("Contents", [])
        for entry in contents:
            store.delete(entry["Key"])
        store.client.delete_bucket(Bucket=settings.s3_bucket)


def test_private_roundtrip_immutable_and_integrity(storage):
    key = f"media/{uuid4()}/{uuid4()}"
    content = b"original binary bytes"
    storage.put(key, content, "application/octet-stream")
    assert storage.stat(key).size == len(content)
    assert storage.read(key, 100, hashlib.sha256(content).hexdigest()) == content
    with pytest.raises(DomainError, match="不能覆盖"):
        storage.put(key, b"replacement", "application/octet-stream")
    assert storage.read(key, 100) == content
    with pytest.raises(DomainError):
        storage.read(key, 2)
    with pytest.raises(DomainError):
        storage.read(key, 100, "0" * 64)
    storage.delete(key)
    storage.delete(key)
    with pytest.raises(DomainError):
        storage.read(key, 100)


@pytest.mark.parametrize("key", ["../escape", "a/../b", "/root", "a//b", "a\\b", "https://x"])
def test_invalid_keys(key):
    with pytest.raises(DomainError):
        safe_key(key)


def test_s3_requires_explicit_credentials_and_endpoint():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, object_storage_backend="s3")


@pytest.mark.parametrize("url", ["file:///tmp", "http://user:pass@localhost", "https://x/?key=1"])
def test_s3_rejects_unsafe_configuration(url):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            object_storage_backend="s3",
            s3_endpoint_url=url,
            s3_access_key="test",
            s3_secret_key="secret",
        )


def test_ready_checks_configured_object_storage(client, monkeypatch):
    client.app.state.settings.object_storage_backend = "s3"

    def unavailable(_self):
        raise DomainError("STORAGE_UNAVAILABLE", "对象存储不可用", 503)

    monkeypatch.setattr(ObjectStorage, "check", unavailable)
    monkeypatch.setattr(ObjectStorage, "__init__", lambda _self, _settings: None)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"


def test_initialization_is_idempotent(storage):
    initialize(storage.settings)
    storage.check()


def test_s3_objects_are_not_public(storage):
    if storage.client is None:
        return
    import httpx

    key = f"private/{uuid4()}"
    storage.put(key, b"private bytes", "application/octet-stream")
    with httpx.Client(trust_env=False) as client:
        response = client.get(
            f"{storage.settings.s3_endpoint_url}/{storage.settings.s3_bucket}/{key}"
        )
    assert response.status_code == 403
    assert b"private bytes" not in response.content


def test_unavailable_s3_does_not_expose_credentials():
    store = ObjectStorage(
        Settings(
            _env_file=None,
            object_storage_backend="s3",
            s3_endpoint_url="http://127.0.0.1:1",
            s3_access_key="do-not-expose-access",
            s3_secret_key="do-not-expose-secret",
        )
    )
    with pytest.raises(DomainError) as result:
        store.check()
    assert result.value.code == "STORAGE_UNAVAILABLE"
    assert "do-not-expose" not in str(result.value)
