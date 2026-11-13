"""Media bytes have no product ceiling; exact receipts and decoding still apply."""

import asyncio
import base64
import hashlib
import random
import struct
from io import BytesIO
from uuid import uuid4

import anyio
import httpx
import pytest
from PIL import Image
from test_asset_versions import draft, versions
from test_assets import create
from test_file_gateway import approved, gateway
from test_media_validation import wave_bytes
from test_native_media_integration import mp4_bytes

from arclight.api.file_transfers import UploadCreate
from arclight.api.protection import RequestProtection
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.file_policy import POLICIES, check_file_policy
from arclight.media.execution import command_for
from arclight.media.validation import probe
from arclight.storage.objects import ObjectStorage


@pytest.fixture
def large_png():
    output = BytesIO()
    pixels = random.Random(73).randbytes(768 * 768 * 3)
    with Image.frombytes("RGB", (768, 768), pixels) as image:
        image.save(output, format="PNG")
    data = output.getvalue()
    assert len(data) > 1024**2
    return data


@pytest.mark.parametrize(
    "media_type,purpose,name",
    [
        ("image/png", "REFERENCE_IMAGE", "scene.png"),
        ("image/jpeg", "REFERENCE_IMAGE", "scene.jpg"),
        ("audio/wav", "AUDIO", "line.wav"),
        ("audio/mpeg", "MUSIC", "theme.mp3"),
        ("audio/ogg", "AUDIO", "line.ogg"),
        ("audio/flac", "GENERATED_AUDIO", "line.flac"),
        ("video/mp4", "GENERATED_VIDEO", "clip.mp4"),
    ],
)
def test_media_receipts_accept_sizes_above_old_limits_and_signed_32_bit(media_type, purpose, name):
    declared_size = 5 * 1024**3
    policy = check_file_policy(purpose, name, media_type, declared_size)
    assert policy.max_bytes is None
    assert command_for("probe", media_type).input_limit is None
    if purpose in {"REFERENCE_IMAGE", "AUDIO", "MUSIC"}:
        upload = UploadCreate(
            purpose=purpose,
            original_name=name,
            media_type=media_type,
            byte_size=declared_size,
            checksum="a" * 64,
        )
        assert upload.byte_size == declared_size
    with pytest.raises(DomainError):
        check_file_policy(purpose, name, media_type, 0)


def test_large_image_roundtrips_through_real_version_upload_and_private_read(
    client, episode, settings, tmp_path, large_png
):
    settings.asset_storage_path = tmp_path / "objects"
    row = create(client, episode["project_id"], "SCENE")
    payload = draft(row, content_base64=base64.b64encode(large_png).decode("ascii"))
    assert len(payload["content_base64"]) > settings.max_request_bytes
    response = client.post(versions(row), json=payload)
    assert response.status_code == 201, response.text
    version = response.json()
    assert version["checksum"] == hashlib.sha256(large_png).hexdigest()
    assert version["media"]["width"] == 768
    assert client.post(versions(row), json=payload).json() == version
    download = client.get(f"{versions(row)}/{version['id']}/image")
    assert download.status_code == 200
    assert download.content == large_png
    assert download.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("remote", [False, True], ids=["local", "s3-stream-contract"])
def test_unlimited_object_read_and_chunked_verify_keep_exact_integrity(tmp_path, large_png, remote):
    store = ObjectStorage(Settings(_env_file=None, asset_storage_path=tmp_path / "objects"))
    key = "generated/test/image.png"
    checksum = hashlib.sha256(large_png).hexdigest()
    if remote:

        class Client:
            def get_object(self, **_kwargs):
                return {"ContentLength": len(large_png), "Body": BytesIO(large_png)}

        store.client = Client()
    else:
        store.put(key, large_png, "image/png")
    assert store.read(key, None, checksum) == large_png
    store.verify(key, len(large_png), checksum, None)
    for wrong_size in (len(large_png) - 1, len(large_png) + 1):
        with pytest.raises(DomainError, match="完整性"):
            store.verify(key, wrong_size, checksum, None)
    with pytest.raises(DomainError, match="完整性"):
        store.read(key, None, "0" * 64)


def test_raw_upload_streams_past_old_32_mib_limit_with_exact_receipt():
    size = 33 * 1024**2
    total = 0
    digest = hashlib.sha256()

    async def authorization(_request):
        return approved(method="PUT", size=size)

    async def storage(request):
        nonlocal total
        assert request.headers["content-length"] == str(size)
        async for part in request.stream:
            assert len(part) <= 64 * 1024
            total += len(part)
            digest.update(part)
        return httpx.Response(200)

    async def exercise():
        app = gateway(authorization, storage)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://gateway"
            ) as client,
        ):

            async def content():
                for _ in range(size // (64 * 1024)):
                    yield b"x" * (64 * 1024)

            response = await client.post(
                "/file-transfers/ticket",
                content=content(),
                headers={"Content-Type": "image/png", "Content-Length": str(size)},
            )
            assert response.status_code == 204, response.text

    anyio.run(exercise)
    assert total == size
    expected = hashlib.sha256()
    for _ in range(size // (64 * 1024)):
        expected.update(b"x" * (64 * 1024))
    assert digest.digest() == expected.digest()


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_real_media_decoder_accepts_valid_containers_above_old_file_limits(kind):
    if kind == "audio":
        original = wave_bytes()
        padding = b"JUNK" + struct.pack("<I", 33 * 1024**2) + bytes(33 * 1024**2)
        data = original + padding
        data = data[:4] + struct.pack("<I", len(data) - 8) + data[8:]
        media_type, minimum = "audio/wav", 32 * 1024**2
    else:
        original = mp4_bytes()
        padding_size = 65 * 1024**2
        data = original + struct.pack(">I", padding_size + 8) + b"free" + bytes(padding_size)
        media_type, minimum = "video/mp4", 64 * 1024**2
    assert len(data) > minimum
    details = probe(data, media_type)
    assert details["media_type"] == media_type
    assert details["duration_ms"] > 0
    with pytest.raises(DomainError):
        probe(data[:-1], media_type)


@pytest.mark.parametrize("route", ["exact", "trailing-slash", "unrelated", "invalid-id"])
def test_media_body_exemption_streams_only_the_actual_version_upload_route(route):
    path = f"/api/v1/projects/{uuid4()}/assets/{uuid4()}/versions"
    valid_media_route = route in {"exact", "trailing-slash"}
    if route == "trailing-slash":
        path += "/"
    elif route == "unrelated":
        path += "/unrelated"
    elif route == "invalid-id":
        path = "/api/v1/projects/invalid/assets/invalid/versions"
    messages, received = [], []

    async def downstream(scope, receive, send):
        while True:
            event = await receive()
            received.append(event["body"])
            if not event.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def exercise():
        events = [
            {"type": "http.request", "body": b"a" * 20, "more_body": True},
            {"type": "http.request", "body": b"b" * 20, "more_body": False},
        ]

        async def receive():
            return events.pop(0)

        async def send(event):
            messages.append(event)

        await RequestProtection(downstream, 10, 30)(
            {"type": "http", "method": "POST", "path": path, "headers": []}, receive, send
        )

    asyncio.run(exercise())
    assert messages[0]["status"] == (200 if valid_media_route else 413)
    assert received == ([b"a" * 20, b"b" * 20] if valid_media_route else [])


def test_default_model_image_capabilities_have_no_product_byte_ceiling():
    declaration = AgentModelCapabilities(version="test:1", supplier="custom", model="vision")
    assert declaration.max_image_bytes is None
    assert declaration.max_total_image_bytes is None
    explicit_supplier_limit = declaration.model_copy(update={"max_image_bytes": 16 * 1024**2})
    assert (
        AgentModelCapabilities.model_validate(explicit_supplier_limit.model_dump()).max_image_bytes
        == 16 * 1024**2
    )
    assert POLICIES["text/plain"].max_bytes == 256 * 1024
