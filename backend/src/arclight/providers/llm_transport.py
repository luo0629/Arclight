"""Bound SDK requests and retain protocol evidence before SDK normalization."""

import json
import zlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote

import httpx

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayFailure, GatewayUsage
from arclight.providers.chat import completed_message, raise_for_provider_status, response_usage
from arclight.providers.native_text import gemini_response, native_usage, qwen_response
from arclight.providers.network import PublicHTTPTransport

RESPONSE_LIMIT = 1024 * 1024
QWEN_TEXT = "/services/aigc/text-generation/generation"
QWEN_VISUAL = "/services/aigc/multimodal-generation/generation"


def gemini_model_path(model: str) -> str:
    return "models/" + quote(model.removeprefix("models/"), safe="")


def turn_url(settings: Settings, *, visual: bool) -> httpx.URL:
    match settings.llm_protocol:
        case "openai_chat":
            suffix = "/chat/completions"
        case "gemini":
            suffix = f"/{gemini_model_path(settings.llm_model)}:generateContent"
        case "qwen_chat":
            suffix = QWEN_VISUAL if visual else QWEN_TEXT
        case _:
            raise GatewayFailure("INPUT_INVALID", not_accepted=True)
    return httpx.URL(settings.llm_base_url.rstrip("/") + suffix)


@dataclass(repr=False)
class Exchange:
    url: httpx.URL
    protocol: Literal["openai_chat", "gemini", "qwen_chat"]
    strict_finish: bool
    attempted: bool = False
    failure: GatewayFailure | None = None
    completion: tuple[dict, str] | None = None
    usage: GatewayUsage = field(default_factory=GatewayUsage)

    def invalid_completion(self) -> GatewayFailure:
        return GatewayFailure("INVALID_RESPONSE", completed=True, usage=self.usage)

    def observe(self, status: int, raw: bytes) -> bytes:
        """Keep exact provider evidence before an SDK drops/coerces wire fields."""
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            body = None
        normalized = body if isinstance(body, dict) else {}
        if self.protocol != "openai_chat":
            normalized = {**normalized, "usage": native_usage(normalized, self.protocol)}
        if normalized.get("code") and not normalized.get("error"):
            normalized = {**normalized, "error": normalized}
        raise_for_provider_status(httpx.Response(status), normalized)
        if not isinstance(body, dict):
            raise GatewayFailure("INVALID_RESPONSE")
        if normalized.get("error"):
            raise_for_provider_status(httpx.Response(400), normalized)
        self.usage = response_usage(normalized)
        if self.protocol == "gemini":
            normalized = gemini_response(body)
        elif self.protocol == "qwen_chat":
            normalized = qwen_response(normalized)
        self.completion = completed_message(normalized, strict=self.strict_finish)
        if self.protocol == "openai_chat" and "role" not in self.completion[0]:
            # Legacy compatible endpoints may omit the completion role. The
            # ChatOpenAI decoder requires it; keep the original evidence above.
            choice = body["choices"][0]
            choice["message"] = {**choice["message"], "role": "assistant"}
            return json.dumps(body, separators=(",", ":")).encode()
        return raw


active_exchange: ContextVar[Exchange | None] = ContextVar("arclight_llm_exchange", default=None)


class LLMTransport(httpx.BaseTransport):
    def __init__(self, dns_timeout: float, public: httpx.BaseTransport | None = None):
        # HTTPcore pools pinned URLs by numeric IP. Never share a pool across origins.
        self._public = public if public is not None else PublicHTTPTransport(dns_timeout)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        turn = active_exchange.get()
        if turn is None:
            raise GatewayFailure("INPUT_INVALID", not_accepted=True)
        try:
            if turn.attempted:
                raise GatewayFailure("PROVIDER_UNAVAILABLE")
            if request.method != "POST" or request.url != turn.url or request.url.query:
                raise GatewayFailure("INVALID_SERVICE_URL", not_accepted=True)
            request.headers["Host"] = turn.url.netloc.decode("ascii")
            request.headers["Accept-Encoding"] = "identity"
            request.headers.pop("Cookie", None)
            turn.attempted = True
            response = self._public.handle_request(request)
            try:
                if response.is_redirect:
                    raise GatewayFailure("SERVICE_REDIRECT_BLOCKED")
                raw = _bounded_body(response)
                headers = httpx.Headers(response.headers)
                for name in ("content-encoding", "transfer-encoding", "set-cookie"):
                    headers.pop(name, None)
                raw = turn.observe(response.status_code, raw)
                if len(raw) > RESPONSE_LIMIT:
                    raise turn.invalid_completion()
                headers["content-length"] = str(len(raw))
                headers["content-type"] = "application/json"
                return httpx.Response(response.status_code, headers=headers, content=raw)
            finally:
                response.close()
        except GatewayFailure as exc:
            turn.failure = exc
            raise
        except DomainError as exc:
            turn.failure = GatewayFailure(
                exc.code,
                not_accepted=exc.code
                in {"INVALID_SERVICE_URL", "SERVICE_DNS_UNAVAILABLE", "SERVICE_ADDRESS_BLOCKED"},
            )
            raise turn.failure from None
        except httpx.RequestError:
            turn.failure = GatewayFailure("PROVIDER_UNAVAILABLE")
            raise turn.failure from None

    def close(self) -> None:
        self._public.close()


def _bounded_body(response: httpx.Response) -> bytes:
    try:
        if int(response.headers.get("content-length", "0")) > RESPONSE_LIMIT:
            raise GatewayFailure("INVALID_RESPONSE")
        if response.is_stream_consumed:  # offline fixture/already buffered transport
            if len(response.content) > RESPONSE_LIMIT:
                raise GatewayFailure("INVALID_RESPONSE")
            return response.content
        encoding = response.headers.get("content-encoding", "identity").lower().strip()
        if encoding not in {"identity", "gzip", "deflate"}:
            raise GatewayFailure("INVALID_RESPONSE")
        decoder = (
            None if encoding == "identity" else zlib.decompressobj(31 if encoding == "gzip" else 15)
        )
        wire_size = 0
        raw = bytearray()
        for chunk in response.iter_raw():
            wire_size += len(chunk)
            if wire_size > RESPONSE_LIMIT:
                raise GatewayFailure("INVALID_RESPONSE")
            if decoder is not None:
                chunk = decoder.decompress(chunk, RESPONSE_LIMIT + 1 - len(raw))
                if decoder.unused_data:
                    raise GatewayFailure("INVALID_RESPONSE")
            raw.extend(chunk)
            if len(raw) > RESPONSE_LIMIT:
                raise GatewayFailure("INVALID_RESPONSE")
        if decoder is not None and not decoder.eof:
            raise GatewayFailure("INVALID_RESPONSE")
        return bytes(raw)
    except (ValueError, zlib.error):
        raise GatewayFailure("INVALID_RESPONSE") from None
