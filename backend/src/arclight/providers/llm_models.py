"""ChatModel construction and the native Gemini and DashScope SDK adapters."""

import base64
import json
import os
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import dashscope
import httpx
from google import genai
from google.genai import types
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from openai import AsyncOpenAI, OpenAI
from pydantic import JsonValue, PrivateAttr, SecretStr

from arclight.core.config import Settings
from arclight.domain.model_gateway import GatewayFailure
from arclight.providers.llm_transport import active_exchange, gemini_model_path
from arclight.providers.native_text import gemini_payload


@dataclass(frozen=True)
class _ChatOptions:
    token_parameter: str = "max_tokens"
    parameters: tuple[tuple[str, JsonValue], ...] = ()


# Wire spelling/defaults only, never a declaration of vision or tool capabilities.
# Unknown compatible endpoints retain the standard request without vendor parameters.
_CHAT_OPTIONS = {
    "openai": ((r".*", _ChatOptions(token_parameter="max_completion_tokens")),),
    "deepseek": ((r".*", _ChatOptions(parameters=(("thinking", {"type": "disabled"}),))),),
    "qwen": ((r"^qwen", _ChatOptions(parameters=(("enable_thinking", False),))),),
}


def invocation_kwargs(
    settings: Settings,
    messages: list[BaseMessage],
    *,
    max_tokens: int,
    response_format: dict,
    tools: list[dict],
) -> dict:
    """Translate common inference options at the SDK boundary, not in the runtime."""
    if settings.llm_protocol != "openai_chat":
        return {"max_tokens": max_tokens, "response_format": response_format, "tools": tools}
    options = next(
        (
            options
            for pattern, options in _CHAT_OPTIONS.get(settings.llm_supplier, ())
            if re.match(pattern, settings.llm_model)
        ),
        _ChatOptions(),
    )
    # extra_body prevents SDK token-field and message-role rewriting for every model.
    extra = {
        **deepcopy(dict(options.parameters)),
        options.token_parameter: max_tokens,
        "messages": message_dicts(messages),
    }
    if response_format:
        extra["response_format"] = response_format
    kwargs: dict = {"extra_body": extra}
    if tools:
        kwargs.update(tools=tools, tool_choice="auto")
    return kwargs


def message_dicts(messages: list[BaseMessage], *, native_ids: bool = False) -> list[dict]:
    values: list[dict] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            values.append({"role": "system", "content": message.content})
        elif isinstance(message, HumanMessage):
            values.append({"role": "user", "content": message.content})
        elif isinstance(message, ToolMessage):
            values.append(
                {"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id}
            )
        elif isinstance(message, AIMessage):
            calls = []
            for call in message.tool_calls:
                identity = call["id"]
                native = message.additional_kwargs.get("native_calls", {}).get(identity, {})
                calls.append(
                    {
                        "type": "function",
                        "id": identity,
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(
                                call["args"], ensure_ascii=False, allow_nan=False
                            ),
                        },
                        **(native if native_ids else {}),
                    }
                )
            values.append(
                {"role": "assistant", "content": message.content or None, "tool_calls": calls}
            )
        else:
            raise GatewayFailure("INPUT_INVALID", not_accepted=True)
    return values


class DisabledAsyncTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise GatewayFailure("INPUT_INVALID", not_accepted=True)


def build_model(
    settings: Settings, http: httpx.Client, async_http: httpx.AsyncClient
) -> tuple[BaseChatModel, Callable[[], None]]:
    base = settings.llm_base_url.rstrip("/")
    key = settings.llm_api_key.get_secret_value()
    timeout = settings.llm_timeout_seconds
    if settings.llm_protocol == "openai_chat":
        # Suppress organization/project defaults, including values set in the SDK environment.
        from openai import Omit

        options: dict[str, Any] = {
            "api_key": key,
            "base_url": base,
            "timeout": timeout,
            "max_retries": 0,
            "organization": "",
            "project": "",
            "admin_api_key": "",
            "default_headers": {"OpenAI-Organization": Omit(), "OpenAI-Project": Omit()},
        }
        sdk = OpenAI(http_client=http, **options)
        async_sdk = AsyncOpenAI(http_client=async_http, **options)
        model = init_chat_model(
            settings.llm_model,
            model_provider="openai",
            api_key=SecretStr(key),
            base_url=base,
            client=sdk.chat.completions,
            root_client=sdk,
            async_client=async_sdk.chat.completions,
            root_async_client=async_sdk,
            http_client=http,
            http_async_client=async_http,
            openai_proxy="",
            http_socket_options=(),
            use_responses_api=False,
            max_retries=0,
            timeout=timeout,
            cache=False,
            verbose=False,
            disable_streaming=True,
        )
        return model, sdk.close
    if settings.llm_protocol == "gemini":
        gemini = genai.Client(
            vertexai=False,
            api_key=key,
            http_options=types.HttpOptions(
                base_url=base,
                api_version="",
                base_url_resource_scope=types.ResourceScope.COLLECTION,
                httpx_client=http,
                httpx_async_client=async_http,
                timeout=int(timeout * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        model = NativeChatModel(
            model_id=settings.llm_model,
            protocol="gemini",
            cache=False,
            verbose=False,
            disable_streaming=True,
        )
        model._sdk = gemini
        return model, gemini.close
    if settings.llm_protocol == "qwen_chat":
        qwen = dashscope.Client(
            protocol="native",
            base_url=base,
            api_key=key,
            workspace="",
            region="cn-beijing",
            config_path=os.devnull,
            http_client=http,
            timeout=timeout,
            max_retries=0,
            strict_validation=False,
        )
        model = NativeChatModel(
            model_id=settings.llm_model,
            protocol="qwen_chat",
            cache=False,
            verbose=False,
            disable_streaming=True,
        )
        model._sdk = qwen
        return model, qwen.close
    raise GatewayFailure("INPUT_INVALID", not_accepted=True)


class NativeChatModel(BaseChatModel):
    model_id: str
    protocol: str
    _sdk: genai.Client | dashscope.Client = PrivateAttr()

    @property
    def _llm_type(self) -> str:
        return self.protocol

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        turn = active_exchange.get()
        if turn is None:
            raise GatewayFailure("INPUT_INVALID", not_accepted=True)
        wire = message_dicts(messages, native_ids=self.protocol == "gemini")
        if isinstance(self._sdk, genai.Client):
            _gemini(self._sdk, self.model_id, wire, **kwargs)
        else:
            _qwen(self._sdk, self.model_id, wire, **kwargs)
        # The transport has the original IDs/opaque bytes and uncoerced token counts,
        # including fields which some SDK response models normalize or omit.
        if turn.completion is None:
            raise turn.invalid_completion()
        message, _ = turn.completion
        content = message.get("content")
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=content if isinstance(content, str) else "",
                        additional_kwargs={"tool_calls": message.get("tool_calls", [])},
                    )
                )
            ]
        )


def _gemini(
    sdk: genai.Client,
    model: str,
    messages: list[dict],
    *,
    max_tokens: int,
    response_format: dict,
    tools: list[dict],
) -> None:
    wire = gemini_payload(
        {
            "messages": messages,
            "max_tokens": max_tokens,
            "response_format": response_format,
            "tools": tools,
        }
    )
    # The SDK spells Blob.mime_type in snake case and decodes opaque signatures.
    # Its public extra_body option preserves the existing Developer API JSON.
    typed_contents = []
    needs_exact_parts = False
    for content in wire["contents"]:
        parts = []
        for value in content["parts"]:
            part = {k: v for k, v in value.items() if k not in {"thoughtSignature", "inlineData"}}
            if "inlineData" in value:
                blob = value["inlineData"]
                part["inline_data"] = types.Blob(
                    data=base64.b64decode(blob["data"], validate=True), mime_type=blob["mimeType"]
                )
            needs_exact_parts |= "thoughtSignature" in value or "inlineData" in value
            parts.append(types.Part.model_validate(part))
        typed_contents.append(types.Content(role=content["role"], parts=parts))
    config = types.GenerateContentConfig(
        system_instruction=types.Content.model_validate(wire["systemInstruction"]),
        max_output_tokens=max_tokens,
        response_mime_type=wire["generationConfig"].get("responseMimeType"),
        response_json_schema=wire["generationConfig"].get("responseJsonSchema"),
        tools=[types.Tool.model_validate(tool) for tool in wire.get("tools", [])] or None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        # Do not decode/re-encode a custom server's opaque thought signatures.
        should_return_http_response=True,
        http_options=types.HttpOptions(extra_body={"contents": wire["contents"]})
        if needs_exact_parts
        else None,
    )
    sdk.models.generate_content(
        model=gemini_model_path(model), contents=typed_contents, config=config
    )


def _qwen(
    sdk: dashscope.Client,
    model: str,
    messages: list[Any],
    *,
    max_tokens: int,
    response_format: dict,
    tools: list[dict],
) -> None:
    schema = response_format.get("json_schema", {}).get("schema")
    if schema:
        messages.insert(
            0,
            {
                "role": "system",
                "content": "JSON Schema:\n" + json.dumps(schema, ensure_ascii=False),
            },
        )
    parameters: dict[str, Any] = {"max_tokens": max_tokens, "enable_thinking": False}
    if tools:
        parameters["tools"] = tools
    else:
        parameters["response_format"] = {"type": "json_object"}
    if any(isinstance(item.get("content"), list) for item in messages):
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = [
                    {"text": item["text"]}
                    if item["type"] == "text"
                    else {"image": item["image_url"]["url"]}
                    for item in content
                ]
            elif isinstance(content, str):
                message["content"] = [{"text": content}]
        sdk.multimodal.chat.create(
            model=model,
            messages=messages,
            stream=False,
            **parameters,
            extra_body={"parameters": {"result_format": "message"}},
        )
    else:
        sdk.chat.completions.create(model=model, messages=messages, stream=False, **parameters)
