"""Bounded native media I/O, private durable outputs and read-only async recovery."""

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.generation_requests import GenerationResult, GenerationSpec
from arclight.domain.model_gateway import GatewayFailure, GatewayUsage
from arclight.providers.chat import raise_for_provider_status
from arclight.providers.media_cancel import CancellationReceipt
from arclight.providers.media_outputs import MediaOutput
from arclight.providers.media_query import ProviderLookup, QueryReceipt
from arclight.providers.media_submission import MediaSubmission, SubmissionReceipt
from arclight.providers.native_media_protocols import (
    ASYNC,
    KINDS,
    capabilities,
    query_path,
    submission,
)
from arclight.providers.network import PublicHTTPTransport, validate_endpoint
from arclight.storage.objects import ObjectStorage


@dataclass(frozen=True)
class MediaConnection:
    configuration: dict
    api_key: str = field(repr=False)


def object_key(handle: str) -> str:
    return "provider-results/native/" + hashlib.sha256(handle.encode()).hexdigest()


def media_kind(data: bytes, fallback: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 255 and data[1] & 224 == 224):
        return "audio/mpeg"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    return fallback


class NativeMediaProvider:
    supports_idempotency = False

    def __init__(
        self,
        settings: Settings,
        connection: MediaConnection,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = settings
        self.connection = connection
        self.name = connection.configuration["protocol"]
        self.capabilities = capabilities(self.name)
        self.transport = transport
        self.store = ObjectStorage(settings)

    def _headers(self) -> dict:
        secret = self.connection.api_key
        return (
            {"x-goog-api-key": secret}
            if self.name in {"gemini_image", "gemini_video", "veo"}
            else {"Authorization": f"Bearer {secret}"}
        )

    def _read(
        self,
        method: str,
        url: str,
        *,
        payload: dict | None = None,
        extra: dict | None = None,
        download: bool = False,
        maximum: int | None = 2097152,
    ) -> tuple[bytes, str, dict]:
        headers = {"Accept-Encoding": "identity"}
        if not download or (
            self.name in {"gemini_image", "gemini_video", "veo"}
            and urlsplit(url).netloc == urlsplit(self.connection.configuration["base_url"]).netloc
        ):
            headers.update(self._headers())
        headers.update(extra or {})
        with (
            httpx.Client(
                timeout=self.settings.media_execution_policy.call_timeout_seconds,
                transport=self.transport
                or PublicHTTPTransport(self.settings.llm_dns_timeout_seconds, allow_query=download),
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream(method, url, json=payload, headers=headers) as response,
        ):
            raw = bytearray()
            for chunk in response.iter_bytes():
                if maximum is not None and len(raw) + len(chunk) > maximum:
                    raise GatewayFailure("INVALID_RESPONSE")
                raw.extend(chunk)
            try:
                error = json.loads(raw) if response.is_error else {}
            except ValueError:
                error = {}
            if isinstance(error, dict) and error.get("code") and not error.get("error"):
                error = {"error": error}
            raise_for_provider_status(response, error if isinstance(error, dict) else {})
            return (
                bytes(raw),
                response.headers.get("content-type", "").split(";")[0],
                dict(response.headers),
            )

    def _json(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        extra: dict | None = None,
        *,
        maximum: int | None = 2097152,
    ) -> dict:
        base = validate_endpoint(self.connection.configuration["base_url"])
        raw, _, _ = self._read(method, base + path, payload=body, extra=extra, maximum=maximum)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise GatewayFailure("INVALID_RESPONSE")
        if method == "POST" and (
            value.get("error") or (value.get("code") and value.get("code") not in {"200", 200})
        ):
            raise_for_provider_status(httpx.Response(400), {"error": value.get("error", value)})
        return value

    def _spec(self, request: MediaSubmission) -> GenerationSpec:
        spec = GenerationSpec.model_validate(request.snapshot["context"]["generation"])
        configuration = self.connection.configuration
        if (
            spec.provider != self.name
            or spec.model != configuration["model"]
            or spec.model_config_version != configuration["configuration_version"]
            or request.snapshot["context"].get("provider_configuration") != configuration
        ):
            raise DomainError("MEDIA_SERVICE_CONFIG_CHANGED", "媒体任务与冻结的账号配置不匹配", 409)
        return spec

    def _success(
        self, request: MediaSubmission, sources: list[tuple[str, str]], usage: dict | None = None
    ) -> GenerationResult:
        task = KINDS[self.name]
        if len(sources) != 1:
            raise GatewayFailure("INVALID_RESPONSE")
        handles = []
        for index, (source, hint) in enumerate(sources):
            if source.startswith("https://"):
                if len(source) > 8192 or urlsplit(source).fragment:
                    raise GatewayFailure("INVALID_RESPONSE")
                data, _, _ = self._read("GET", source, download=True, maximum=None)
            else:
                data = base64.b64decode(source, validate=True)
            if not data:
                raise GatewayFailure("INVALID_RESPONSE")
            kind = media_kind(data, hint)
            suffix = {
                "image/png": "png",
                "image/jpeg": "jpeg",
                "audio/wav": "wav",
                "audio/mpeg": "mp3",
                "audio/ogg": "opus",
                "audio/flac": "flac",
                "video/mp4": "mp4",
            }.get(kind)
            if suffix is None:
                raise GatewayFailure("INVALID_RESPONSE")
            checksum = hashlib.sha256(data).hexdigest()
            handle = (
                f"native/{self.name}/{request.job_id.hex}/{request.attempt_id.hex}/"
                f"{index}/{checksum}.{suffix}"
            )
            key = object_key(handle)
            try:
                self.store.put(key, data, kind)
            except DomainError as exc:
                if exc.code != "OBJECT_EXISTS" or self.store.read(key, None, checksum) != data:
                    raise GatewayFailure("STORAGE_FAILED") from None
            handles.append(handle)
        measured: dict[str, JsonValue] = {"images": 1} if task == "IMAGE" else {}
        for key, value in (usage if isinstance(usage, dict) else {}).items():
            if (
                key in {"input_tokens", "output_tokens", "total_tokens", "seconds", "characters"}
                and type(value) in {int, float}
                and 0 <= value <= 10**10
            ):
                measured[key] = value
        return GenerationResult(output_handles=handles, usage=measured)

    def _sources(self, value: dict) -> list[tuple[str, str]]:
        if self.name == "gemini_image":
            candidates = value.get("candidates", [])
            return [
                (part["inlineData"]["data"], part["inlineData"].get("mimeType", "image/png"))
                for candidate in candidates
                for part in candidate.get("content", {}).get("parts", [])
                if "inlineData" in part
            ]
        if self.name == "gemini_video":
            return [
                (part.get("uri") or part.get("data", ""), part.get("mime_type", "video/mp4"))
                for step in value.get("steps", [])
                if step.get("type") == "model_output"
                for part in step.get("content", [])
                if part.get("type") == "video"
            ]
        if self.name == "qwen_tts":
            return [(value["output"]["audio"]["url"], "audio/wav")]
        items = (
            value.get("images", []) if self.name == "siliconflow_image" else value.get("data", [])
        )
        return [(item.get("b64_json") or item.get("url", ""), "image/png") for item in items]

    def submit(self, request: MediaSubmission) -> SubmissionReceipt:
        completed = False
        try:
            spec = self._spec(request)
            path, body, extra = submission(spec)
            self.store.check()
        except (DomainError, KeyError, ValueError):
            raise GatewayFailure("INPUT_INVALID", not_accepted=True) from None
        try:
            if self.name in {"openai_tts", "siliconflow_tts"}:
                raw, kind, _ = self._read(
                    "POST",
                    self.connection.configuration["base_url"] + path,
                    payload=body,
                    extra=extra,
                    maximum=None,
                )
                completed = True
                fallback = {
                    "wav": "audio/wav",
                    "mp3": "audio/mpeg",
                    "opus": "audio/ogg",
                    "flac": "audio/flac",
                }[cast(str, spec.output_spec.get("format", "wav"))]
                result = self._success(
                    request,
                    [
                        (
                            base64.b64encode(raw).decode(),
                            kind if kind.startswith("audio/") else fallback,
                        )
                    ],
                )
                return SubmissionReceipt(status="SUCCEEDED", result=result)
            value = self._json(
                "POST",
                path,
                body,
                extra,
                maximum=None
                if self.name not in ASYNC and KINDS[self.name] in {"IMAGE", "VIDEO"}
                else 2097152,
            )
            if self.name in ASYNC:
                identity = value.get("id") or value.get("request_id")
                if self.name == "wan":
                    identity = value.get("output", {}).get("task_id")
                elif self.name == "veo":
                    name = value.get("name", "")
                    if not re.fullmatch(
                        r"(?:models/[A-Za-z0-9_-]+/)?operations/[A-Za-z0-9_-]+", name
                    ):
                        raise GatewayFailure("INVALID_RESPONSE")
                    identity = "op_" + base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
                if not isinstance(identity, str) or not re.fullmatch(
                    r"[A-Za-z0-9_.:-]{1,200}", identity
                ):
                    raise GatewayFailure("INVALID_RESPONSE")
                return SubmissionReceipt(status="SUBMITTED", request_id=identity)
            completed = True
            return SubmissionReceipt(
                status="SUCCEEDED",
                result=self._success(request, self._sources(value), value.get("usage")),
            )
        except (httpx.HTTPError, DomainError, KeyError, TypeError, ValueError, AttributeError):
            raise GatewayFailure(
                "INVALID_RESPONSE",
                completed=completed,
                usage=GatewayUsage(images=1 if KINDS[self.name] == "IMAGE" else None)
                if completed
                else None,
            ) from None
        except GatewayFailure as exc:
            if not completed:
                raise
            # A failed result download cannot turn accepted generation into a free rejection.
            raise GatewayFailure(
                "INVALID_RESPONSE",
                completed=True,
                usage=GatewayUsage(images=1 if KINDS[self.name] == "IMAGE" else None),
            ) from exc

    def query(self, lookup: ProviderLookup) -> QueryReceipt:
        request = lookup.submission
        if self.name not in ASYNC or request is None:
            return QueryReceipt(status="UNSUPPORTED")
        try:
            spec = self._spec(request)
            if lookup.provider != self.name or lookup.model != spec.model:
                return QueryReceipt(status="INVALID_RESPONSE")
            value = self._json("GET", query_path(self.name, lookup.request_id))
            output = value.get("output", value)
            status = str(
                output.get("task_status", value.get("task_status", value.get("status", "")))
            ).lower()
            if self.name == "veo":
                if not value.get("done"):
                    return QueryReceipt(status="RUNNING")
                if value.get("error"):
                    return QueryReceipt(status="FAILED", error_code="PROVIDER_FAILED")
                videos = (
                    value.get("response", {})
                    .get("generateVideoResponse", {})
                    .get("generatedSamples", [])
                )
                sources = [(item["video"]["uri"], "video/mp4") for item in videos]
            else:
                if status in {"queued", "running", "processing", "pending", "submitted"}:
                    return QueryReceipt(status="RUNNING")
                if status in {"cancelled", "canceled"}:
                    return QueryReceipt(status="CANCELLED")
                if status in {"failed", "fail"}:
                    return QueryReceipt(status="FAILED", error_code="PROVIDER_FAILED")
                if status not in {"succeeded", "success", "done"}:
                    return QueryReceipt(status="UNKNOWN")
                if self.name == "seedance":
                    sources = [(value["content"]["video_url"], "video/mp4")]
                elif self.name == "wan":
                    sources = [(output["video_url"], "video/mp4")]
                elif self.name == "cogvideo":
                    sources = [(item["url"], "video/mp4") for item in value["video_result"]]
                else:
                    if value.get("video", {}).get("respect_moderation") is False:
                        return QueryReceipt(status="FAILED", error_code="PROVIDER_REJECTED")
                    sources = [(value["video"]["url"], "video/mp4")]
            return QueryReceipt(
                status="SUCCEEDED", result=self._success(request, sources, value.get("usage"))
            )
        except (httpx.HTTPError, DomainError, GatewayFailure):
            return QueryReceipt(status="QUERY_FAILED")
        except (KeyError, TypeError, ValueError, AttributeError):
            return QueryReceipt(status="INVALID_RESPONSE")

    def cancel(self, lookup: ProviderLookup) -> CancellationReceipt:
        # These adapters only declare authoritative query support. A local cancel
        # intent never becomes a fabricated remote cancellation confirmation.
        return CancellationReceipt(status="UNSUPPORTED")

    def download(self, request: MediaSubmission, handle: str, max_bytes: int | None) -> MediaOutput:
        prefix = f"native/{self.name}/{request.job_id.hex}/{request.attempt_id.hex}/"
        match = re.fullmatch(
            re.escape(prefix) + r"0/([a-f0-9]{64})\.(png|jpeg|wav|mp3|opus|flac|mp4)", handle
        )
        if match is None:
            raise DomainError("MEDIA_OUTPUT_UNAVAILABLE", "结果标识不属于当前媒体尝试", 422)
        kind = {
            "png": "image/png",
            "jpeg": "image/jpeg",
            "wav": "audio/wav",
            "mp3": "audio/mpeg",
            "opus": "audio/ogg",
            "flac": "audio/flac",
            "mp4": "video/mp4",
        }[match[2]]
        return MediaOutput(self.store.read(object_key(handle), max_bytes, match[1]), kind)
