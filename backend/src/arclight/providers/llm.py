"""Synchronous, gateway-only inference with a bounded process-local model pool."""

import asyncio
import atexit
import hmac
import json
import os
import re
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock

import httpx
from langchain_core.globals import set_debug
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langsmith import tracing_context
from pydantic import BaseModel

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayFailure, GatewayUsage
from arclight.providers.llm_transport import Exchange, LLMTransport, active_exchange, turn_url
from arclight.providers.network import validate_endpoint
from arclight.providers.sdk_logging import suppress_sdk_logging


@dataclass(repr=False)
class _Admission:
    settings: Settings
    credential_identity: tuple[str, ...]
    used: bool = False
    usage: GatewayUsage = field(default_factory=GatewayUsage)


_admission: ContextVar[_Admission | None] = ContextVar("arclight_llm_admission", default=None)


def dispatch_inference[T](
    settings: Settings, credential_identity: tuple[str, ...], call: Callable[[], T]
) -> tuple[T, GatewayUsage]:
    """Run in the gateway's I/O thread, after committing admission."""
    admission = _Admission(settings, credential_identity)
    reset = _admission.set(admission)
    try:
        return call(), admission.usage
    finally:
        _admission.reset(reset)


@dataclass(frozen=True, repr=False)
class LLMCompletion:
    message: dict
    reason: str
    usage: GatewayUsage


@dataclass(frozen=True)
class _ModelKey:
    protocol: str
    supplier: str
    endpoint: str
    model: str
    timeout: float
    dns_timeout: float
    credential: bytes = field(repr=False)


@dataclass(repr=False)
class _Entry:
    model: BaseChatModel
    close: Callable[[], None]
    users: int = 0
    retired: bool = False


def _close_async(client: httpx.AsyncClient) -> None:
    # Async transports never open sockets; they are injected to prevent an SDK
    # from constructing a second, unrestricted client.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(client.aclose())
    else:
        loop.create_task(client.aclose())


class LLMService:
    def __init__(self, *, capacity: int = 16, offline_transport: httpx.MockTransport | None = None):
        if capacity < 1:
            raise ValueError("Model pool capacity must be positive")
        if offline_transport is not None and type(offline_transport) is not httpx.MockTransport:
            raise TypeError("Offline inference requires MockTransport")
        self._capacity = capacity
        self._offline_transport = offline_transport
        self._reset_pool()

    def _reset_pool(self) -> None:
        self._lock = Lock()
        self._secret = os.urandom(32)
        self._entries: OrderedDict[_ModelKey, _Entry] = OrderedDict()

    def clear(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            for entry in entries:
                entry.retired = True
                if not entry.users:
                    entry.close()

    @contextmanager
    def _model(self, settings: Settings, identity: tuple[str, ...]) -> Iterator[BaseChatModel]:
        key = _ModelKey(
            settings.llm_protocol,
            settings.llm_supplier,
            settings.llm_base_url,
            settings.llm_model,
            settings.llm_timeout_seconds,
            settings.llm_dns_timeout_seconds,
            hmac.digest(
                self._secret,
                json.dumps(
                    [
                        *identity,
                        settings.llm_api_key.get_secret_value(),
                    ]
                ).encode(),
                "sha256",
            ),
        )
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._build(settings)
                self._entries[key] = entry
                if len(self._entries) > self._capacity:
                    _, evicted = self._entries.popitem(last=False)
                    evicted.retired = True
                    if not evicted.users:
                        evicted.close()
            else:
                self._entries.move_to_end(key)
            entry.users += 1
        try:
            yield entry.model
        finally:
            with self._lock:
                entry.users -= 1
                if entry.retired and not entry.users:
                    entry.close()

    def _build(self, settings: Settings) -> _Entry:
        from arclight.providers.llm_models import DisabledAsyncTransport, build_model

        suppress_sdk_logging()
        http = httpx.Client(
            transport=LLMTransport(settings.llm_dns_timeout_seconds, self._offline_transport),
            timeout=settings.llm_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )
        async_http = httpx.AsyncClient(transport=DisabledAsyncTransport(), trust_env=False)
        try:
            model, close_sdk = build_model(settings, http, async_http)
        except Exception:
            http.close()
            _close_async(async_http)
            raise

        def close() -> None:
            try:
                close_sdk()
            finally:
                http.close()
                _close_async(async_http)

        return _Entry(model, close)

    def invoke(
        self,
        settings: Settings,
        messages: list[BaseMessage],
        *,
        max_tokens: int,
        response_format: dict,
        tools: list[dict] | None = None,
        strict_finish: bool = True,
    ) -> LLMCompletion:
        from arclight.providers.llm_models import message_dicts

        admission = _admission.get()
        identity: tuple[str, ...]
        if admission is None:
            if self._offline_transport is None:
                raise DomainError("GATEWAY_ADMISSION_REQUIRED", "文字推理必须经模型网关受理", 409)
            identity = ("OFFLINE",)
        else:
            if admission.settings is not settings:
                raise GatewayFailure("INPUT_INVALID", not_accepted=True)
            if admission.used:
                raise GatewayFailure("PROVIDER_UNAVAILABLE")
            admission.used = True
            identity = admission.credential_identity
        if not settings.llm_api_key.get_secret_value():
            raise GatewayFailure("PROVIDER_AUTHENTICATION_FAILED", not_accepted=True)
        try:
            base = validate_endpoint(settings.llm_base_url)
        except DomainError as exc:
            raise GatewayFailure(exc.code, not_accepted=True) from None
        settings = settings.model_copy(update={"llm_base_url": base})
        visual = _has_inline_images(messages)
        turn = Exchange(turn_url(settings, visual=visual), settings.llm_protocol, strict_finish)
        tools = tools or []
        kwargs: dict = {
            "max_tokens": max_tokens,
            "response_format": response_format,
            "tools": tools,
        }
        if settings.llm_protocol == "openai_chat":
            token_parameter = (
                "max_completion_tokens" if settings.llm_supplier == "openai" else "max_tokens"
            )
            # Public SDK extra_body avoids ChatOpenAI max_tokens renaming and the
            # SDK parse() path which requires strict tools even in json_object mode.
            extra = {token_parameter: max_tokens, "response_format": response_format}
            if settings.llm_supplier == "deepseek":
                extra["thinking"] = {"type": "disabled"}
            if settings.llm_supplier == "qwen" and settings.llm_model.startswith("qwen"):
                # Match the native Qwen path; hybrid models can default to thinking.
                extra["enable_thinking"] = False
            if re.match(r"^o\d", settings.llm_model):
                # Keep the historical system role for custom o-series gateways.
                extra["messages"] = message_dicts(messages)
            kwargs = {"extra_body": extra}
            if tools:
                kwargs.update(tools=tools, tool_choice="auto")
        reset = active_exchange.set(turn)
        try:
            set_debug(False)
            with (
                self._model(settings, identity) as model,
                tracing_context(enabled=False, parent=None),
            ):
                answer = model.invoke(messages, config={"callbacks": []}, **kwargs)
            if not isinstance(answer, AIMessage) or turn.completion is None:
                raise turn.invalid_completion()
            message, reason = turn.completion
            if reason == "stop" and isinstance(message.get("content"), str):
                if answer.content != message["content"]:
                    raise turn.invalid_completion()
                message = {**message, "content": answer.content}
            if admission is not None:
                admission.usage = turn.usage
            return LLMCompletion(message, reason, turn.usage)
        except GatewayFailure:
            raise
        except Exception:
            if turn.failure is not None:
                raise turn.failure from None
            if turn.completion is not None:
                raise turn.invalid_completion() from None
            raise GatewayFailure(
                "PROVIDER_UNAVAILABLE" if turn.attempted else "INPUT_INVALID",
                not_accepted=not turn.attempted,
            ) from None
        finally:
            active_exchange.reset(reset)


def response_contract(contract: type[BaseModel], settings: Settings) -> tuple[dict, dict]:
    schema = contract.model_json_schema()
    fmt = {"type": settings.llm_response_format}
    if settings.llm_response_format == "json_schema":
        return {**fmt, "json_schema": {"name": contract.__name__, "schema": schema}}, schema
    return fmt, schema


def _has_inline_images(messages: list[BaseMessage]) -> bool:
    visual = False
    for message in messages:
        if not isinstance(message, HumanMessage) or isinstance(message.content, str):
            continue
        for part in message.content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                continue
            if isinstance(part, dict) and part.get("type") == "image_url":
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else None
                if isinstance(url, str) and url.startswith(
                    ("data:image/png;base64,", "data:image/jpeg;base64,")
                ):
                    visual = True
                    continue
            raise GatewayFailure("INPUT_INVALID", not_accepted=True)
    return visual


llm_service = LLMService()
atexit.register(llm_service.clear)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=llm_service._reset_pool)
