"""Media envelopes and private downloads have no project byte ceiling."""

import base64
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr, ValidationError
from test_native_providers import native_request
from test_openai_images import image_request, response

from arclight.core.errors import DomainError
from arclight.core.image_service import OpenAIImageSettings
from arclight.domain.candidate_checks import CheckedImage
from arclight.domain.model_gateway import GatewayFailure
from arclight.providers.media_submission import MediaSubmission
from arclight.providers.native_media import NativeMediaProvider
from arclight.providers.openai_images import OpenAIImageProvider


@pytest.fixture(scope="module")
def large_png():
    stream = BytesIO()
    Image.new("RGB", (1024, 1024), (15, 80, 190)).save(stream, "PNG", compress_level=0)
    data = stream.getvalue()
    assert len(data) > 1024**2
    assert len(base64.b64encode(data)) > 2 * 1024**2
    return data


def test_openai_accepts_large_inline_image_and_preserves_exact_private_bytes(
    settings, tmp_path, large_png
):
    settings.asset_storage_path = tmp_path / "images"
    settings.asset_storage_path.mkdir()
    settings.image_service = OpenAIImageSettings(
        enabled=True,
        api_key=SecretStr("synthetic-provider-key"),
        maximum_cost_per_image=Decimal("0.5"),
        pricing_version="synthetic-price:1",
    )
    spec = image_request(settings)["spec"]
    spec["output_spec"]["format"] = "png"
    request = MediaSubmission(
        uuid4(),
        uuid4(),
        uuid4(),
        {
            "context": {
                "generation": spec,
                "provider_configuration": settings.image_service.public_configuration(),
            }
        },
        None,
    )
    provider = OpenAIImageProvider(
        settings,
        transport=httpx.MockTransport(
            lambda _: response({"data": [{"b64_json": base64.b64encode(large_png).decode()}]})
        ),
    )
    receipt = provider.submit(request)
    assert receipt.status == "SUCCEEDED" and receipt.result
    handle = receipt.result.output_handles[0]
    assert provider.download(request, handle, None).data == large_png
    assert provider.submit(request).result == receipt.result
    with pytest.raises(DomainError):
        provider.download(request, handle, 1024)


@pytest.mark.parametrize(
    "protocol,source",
    [("openai_image", "inline"), ("openai_image", "url"), ("gemini_image", "inline")],
)
def test_native_large_images_keep_inline_and_url_outputs_exact(
    settings, tmp_path, large_png, protocol, source
):
    connection, request = native_request(settings, tmp_path, protocol)

    def handler(incoming):
        if incoming.method == "GET":
            assert source == "url"
            assert "authorization" not in incoming.headers
            return httpx.Response(200, content=large_png)
        encoded = base64.b64encode(large_png).decode()
        if protocol == "gemini_image":
            payload = {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"inlineData": {"data": encoded, "mimeType": "image/png"}}]
                        }
                    }
                ]
            }
        else:
            payload = {
                "data": [
                    {"url": "https://cdn.example/large.png"}
                    if source == "url"
                    else {"b64_json": encoded}
                ]
            }
        return httpx.Response(200, json=payload)

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    receipt = provider.submit(request)
    assert receipt.status == "SUCCEEDED" and receipt.result
    handle = receipt.result.output_handles[0]
    assert provider.download(request, handle, None).data == large_png
    assert provider.submit(request).result == receipt.result
    with pytest.raises(DomainError):
        provider.download(request, handle, 1024)


def test_async_control_json_keeps_its_nonmedia_response_limit(settings, tmp_path):
    connection, request = native_request(settings, tmp_path, "wan")
    provider = NativeMediaProvider(
        settings,
        connection,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"output": {"task_id": "task_1"}, "control": "x" * (2 * 1024**2)}
            )
        ),
    )
    with pytest.raises(GatewayFailure) as failure:
        provider.submit(request)
    assert failure.value.code == "INVALID_RESPONSE"


def test_candidate_image_evidence_accepts_large_bytes_but_keeps_image_constraints():
    evidence = {
        "checksum": "a" * 64,
        "media_type": "image/png",
        "byte_size": 5 * 1024**3,
        "width": 1024,
        "height": 1024,
    }
    assert CheckedImage.model_validate(evidence).byte_size == 5 * 1024**3
    for invalid in ({"byte_size": 0}, {"width": 4097}, {"width": 3000, "height": 3000}):
        with pytest.raises(ValidationError):
            CheckedImage.model_validate(evidence | invalid)
