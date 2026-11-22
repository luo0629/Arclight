"""Read-only connection checks for the model service settings form."""

import json
from dataclasses import dataclass
from functools import partial
from threading import BoundedSemaphore
from typing import Literal

import httpx

from arclight.core.errors import DomainError
from arclight.providers.llm_transport import gemini_model_path
from arclight.providers.media_io import bounded_call
from arclight.providers.network import PublicHTTPTransport

_MODEL_LIST_PROTOCOLS = {
    "openai_chat",
    "openai_image",
    "openai_tts",
    "siliconflow_image",
    "siliconflow_tts",
}
_GEMINI_MODEL_PROTOCOLS = {"gemini", "gemini_image", "gemini_video", "veo"}
_PROBE_SLOTS = BoundedSemaphore(4)
_RESPONSE_LIMIT = 512 * 1024


@dataclass(frozen=True)
class ConnectionCheck:
    status: Literal["model_found", "reachable", "failed"]
    message: str


def _model_found(response: httpx.Response, model: str, *, gemini: bool) -> bool:
    raw = bytearray()
    # Only inspect bounded, uncompressed metadata. Provider bodies stay server-side.
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        return False
    for chunk in response.iter_raw(chunk_size=16 * 1024):
        if len(raw) + len(chunk) > _RESPONSE_LIMIT:
            return False
        raw.extend(chunk)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    if not isinstance(payload, dict):
        return False
    if gemini:
        return payload.get("name") == "models/" + model.removeprefix("models/")
    models = payload.get("data")
    return isinstance(models, list) and any(
        isinstance(row, dict) and row.get("id") == model for row in models
    )


def _check(
    base_url: str, protocol: str, model: str, api_key: str, dns_timeout: float
) -> ConnectionCheck:
    gemini = protocol in _GEMINI_MODEL_PROTOCOLS
    suffix = ""
    headers = {"Accept-Encoding": "identity"}
    if gemini:
        if model.removeprefix("models/") in {"", ".", ".."}:
            raise DomainError("INVALID_SERVICE_MODEL", "请填写有效的模型 ID", 422)
        suffix = "/" + gemini_model_path(model)
        headers["x-goog-api-key"] = api_key
    else:
        if protocol in _MODEL_LIST_PROTOCOLS:
            suffix = "/models"
        headers["Authorization"] = "Bearer " + api_key

    with (
        httpx.Client(
            transport=PublicHTTPTransport(min(dns_timeout, 4)),
            timeout=4,
            trust_env=False,
            follow_redirects=False,
        ) as client,
        client.stream("GET" if suffix else "HEAD", base_url + suffix, headers=headers) as response,
    ):
        status = response.status_code
        if status in (401, 403):
            return ConnectionCheck("failed", "服务拒绝访问，请检查 API Key 和账号权限。")
        if status == 429:
            return ConnectionCheck("failed", "服务商限制了请求频率，请稍后重试。")
        if status >= 400 and status not in (404, 405, 501):
            return ConnectionCheck(
                "failed", f"服务未通过检测（HTTP {status}），请检查地址、协议和凭证。"
            )
        if not suffix:
            return ConnectionCheck("reachable", "服务可达。当前协议的检测仅确认地址有响应。")
        if response.is_success and _model_found(response, model, gemini=gemini):
            return ConnectionCheck(
                "model_found", "服务可达，已查到该模型。生成权限以实际调用为准。"
            )
        return ConnectionCheck(
            "reachable", "服务可达，模型查询未能确认此模型。请核对 Base URL、协议和模型 ID。"
        )


def probe_connection(
    base_url: str, protocol: str, model: str, api_key: str, dns_timeout: float
) -> ConnectionCheck:
    if not api_key or any(not 33 <= ord(char) <= 126 for char in api_key):
        raise DomainError("INVALID_SERVICE_KEY", "请检查 API Key 是否包含空格或无效字符", 422)
    try:
        return bounded_call(
            partial(_check, base_url, protocol, model, api_key, dns_timeout), 8, slots=_PROBE_SLOTS
        )
    except DomainError as exc:
        if exc.code == "SERVICE_REDIRECT_BLOCKED":
            return ConnectionCheck(
                "reachable", "服务已响应，但检测遇到重定向。请按服务商文档核对 Base URL。"
            )
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise DomainError("SERVICE_PROBE_TIMEOUT", "连接检测超时或繁忙，请稍后重试", 503) from None
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
        raise DomainError(
            "SERVICE_PROBE_UNAVAILABLE", "无法连接服务，请检查 Base URL 和服务端网络", 502
        ) from None
