"""Bounded synchronous OpenAI images; durable private handles, never response URLs."""

import base64
import binascii
import hashlib
import re
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.asset_versions import MAX_IMAGE_BASE64, MAX_IMAGE_BYTES
from arclight.domain.generation_requests import GenerationResult, GenerationSpec
from arclight.domain.media_capabilities import validate_capabilities
from arclight.domain.model_gateway import GatewayFailure
from arclight.providers.media_capabilities import OPENAI_IMAGE_CAPABILITIES, resolve_parameters
from arclight.providers.media_outputs import MediaOutput
from arclight.providers.media_submission import MediaSubmission, SubmissionReceipt
from arclight.providers.network import PublicHTTPTransport
from arclight.storage.objects import ObjectStorage


def validate_spec(spec: GenerationSpec) -> None:
    validate_capabilities(OPENAI_IMAGE_CAPABILITIES, spec, "IMAGE", 0)
    quality = spec.parameters.get("quality", "low")
    kind = spec.output_spec.get("format", "png")
    if (
        set(spec.parameters) - resolve_parameters(spec.provider, spec.model)
        or not isinstance(quality, str)
        or quality not in {"low", "medium", "high"}
        or set(spec.output_spec) - {"width", "height", "format"}
        or not isinstance(kind, str)
        or kind not in {"png", "jpeg", "jpg"}
    ):
        raise DomainError("PROVIDER_PARAMETERS_UNSUPPORTED", "图像参数或格式不受支持", 422)


class ImageData(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    b64_json: str = Field(min_length=1, max_length=MAX_IMAGE_BASE64, repr=False)


class ImageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    data: list[ImageData] = Field(min_length=1, max_length=4)
    usage: dict = Field(default_factory=dict)


def result_object_key(handle: str) -> str:
    # Keep filesystem keys short even when the private handle binds both UUIDs.
    return "provider-results/openai/" + hashlib.sha256(handle.encode()).hexdigest()


class OpenAIImageProvider:
    name = "openai"
    supports_idempotency = False
    capabilities = OPENAI_IMAGE_CAPABILITIES

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self.store = ObjectStorage(settings)

    def submit(self, request: MediaSubmission) -> SubmissionReceipt:
        service = self.settings.image_service
        try:
            service.require_ready()
            spec = GenerationSpec.model_validate(request.snapshot["context"]["generation"])
            validate_spec(spec)
            if (
                spec.model != service.model
                or spec.model_config_version
                != service.public_configuration()["configuration_version"]
                or request.snapshot["context"].get("provider_configuration")
                != service.public_configuration()
            ):
                raise DomainError("IMAGE_SERVICE_CONFIG_CHANGED", "图像服务配置已变化", 409)
            secret = service.secret()
            self.store.check()
        except (DomainError, KeyError, ValidationError):
            raise GatewayFailure("INPUT_INVALID", not_accepted=True) from None

        output = spec.output_spec
        kind = "jpeg" if output.get("format") in {"jpeg", "jpg"} else "png"
        prompt = spec.prompt
        if spec.negative_prompt:
            prompt += "\n\nAvoid these elements:\n" + spec.negative_prompt
        body = {
            "model": spec.model,
            "prompt": prompt,
            "size": f"{output['width']}x{output['height']}",
            "quality": spec.parameters.get("quality", "low"),
            "n": spec.batch_size,
            "output_format": kind,
            **({"output_compression": 85} if kind == "jpeg" else {}),
        }
        started = time.monotonic()
        try:
            with (
                httpx.Client(
                    timeout=service.timeout_seconds,
                    transport=self.transport or PublicHTTPTransport(service.dns_timeout_seconds),
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "POST",
                    service.base_url + "/images/generations",
                    headers={"Authorization": f"Bearer {secret}", "Accept-Encoding": "identity"},
                    json=body,
                ) as response,
            ):
                if response.status_code in {401, 403, 404, 422, 429}:
                    raise GatewayFailure(
                        "PROVIDER_UNAVAILABLE",
                        not_accepted=True,
                        transient=response.status_code == 429,
                    )
                if response.status_code != 200 or response.headers.get(
                    "content-encoding", "identity"
                ) not in {"", "identity"}:
                    raise GatewayFailure("INVALID_RESPONSE")
                maximum = spec.batch_size * MAX_IMAGE_BASE64 + 16384
                raw = bytearray()
                for chunk in response.iter_raw(chunk_size=65536):
                    if len(raw) + len(chunk) > maximum:
                        raise GatewayFailure("INVALID_RESPONSE")
                    raw.extend(chunk)
                result = ImageResponse.model_validate_json(raw)
                request_id = response.headers.get("x-request-id")
        except (httpx.HTTPError, DomainError, ValidationError, ValueError):
            # A redirect, timeout, malformed or lost response cannot prove rejection.
            raise GatewayFailure("INVALID_RESPONSE") from None
        if len(result.data) != spec.batch_size:
            raise GatewayFailure("INVALID_RESPONSE")
        usage: dict[str, JsonValue] = {"images": len(result.data)}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = result.usage.get(key)
            if value is not None:
                if type(value) is not int or not 0 <= value <= 10**10:
                    raise GatewayFailure("INVALID_RESPONSE")
                usage[key] = value
        handles = []
        for index, item in enumerate(result.data):
            try:
                data = base64.b64decode(item.b64_json, validate=True)
                if not data or len(data) > MAX_IMAGE_BYTES:
                    raise ValueError
            except (ValueError, binascii.Error):
                raise GatewayFailure("INVALID_RESPONSE") from None
            checksum = hashlib.sha256(data).hexdigest()
            handle = (
                f"openai/image/{request.job_id.hex}/{request.attempt_id.hex}/"
                f"{index}/{checksum}.{kind}"
            )
            key = result_object_key(handle)
            try:
                self.store.put(key, data, "image/" + kind)
            except DomainError as exc:
                if exc.code != "OBJECT_EXISTS":
                    raise GatewayFailure("PROVIDER_FAILED") from None
                if self.store.read(key, MAX_IMAGE_BYTES, checksum) != data:
                    raise GatewayFailure("INVALID_RESPONSE") from None
            handles.append(handle)
        # Dollar charges are not returned by Images. Tokens never become an invoice.
        return SubmissionReceipt(
            status="SUCCEEDED",
            request_id=request_id
            if request_id and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", request_id)
            else None,
            result=GenerationResult(output_handles=handles, usage=usage),
            latency_ms=min(int((time.monotonic() - started) * 1000), 2147483647),
            cost=None,
        )

    def download(self, request: MediaSubmission, handle: str, max_bytes: int) -> MediaOutput:
        prefix = f"openai/image/{request.job_id.hex}/{request.attempt_id.hex}/"
        match = re.fullmatch(re.escape(prefix) + r"([0-3])/([a-f0-9]{64})\.(png|jpeg)", handle)
        if match is None:
            raise DomainError("MEDIA_OUTPUT_UNAVAILABLE", "图像结果标识不属于当前尝试", 422)
        checksum, kind = match[2], match[3]
        return MediaOutput(
            self.store.read(result_object_key(handle), min(max_bytes, MAX_IMAGE_BYTES), checksum),
            "image/" + kind,
        )
