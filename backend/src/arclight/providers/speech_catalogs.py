"""Read voice catalogs using the configured speech protocol and connection."""

import json
import re
from collections.abc import Callable
from functools import lru_cache, partial
from time import monotonic

import httpx

from arclight.domain.audio_voices import AudioVoiceOption
from arclight.domain.model_gateway import GatewayFailure
from arclight.providers.media_io import JSON_RESPONSE_LIMIT, read_provider_response
from arclight.providers.network import PublicHTTPTransport
from arclight.providers.speech_catalog_documents import (
    OPENAI_VOICE_DOCUMENT,
    QWEN_AUDIO_VOICE_DOCUMENT,
    QWEN_VOICE_DOCUMENT,
    SILICONFLOW_VOICE_DOCUMENT,
    VoiceOptions,
    openai_document_voices,
    qwen_audio_document_voices,
    qwen_document_voices,
    siliconflow_document_voices,
)

VoiceCatalogQuery = Callable[[str, float, float], VoiceOptions]

_DOC_CACHE_SECONDS = 300
_WORKSPACE_ROUTE = re.compile(
    r"https://[a-z0-9-]+\.(cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com/api/v1"
)


def _read_catalog(
    url: str,
    timeout: float,
    dns_timeout: float,
    *,
    key: str | None = None,
    model: str | None = None,
    optional: bool = False,
) -> bytes | None:
    headers = {"Accept-Encoding": "identity"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
        headers["Accept"] = "application/json"
    with (
        httpx.Client(
            timeout=timeout,
            transport=PublicHTTPTransport(dns_timeout, allow_query=model is not None),
            trust_env=False,
            follow_redirects=False,
        ) as client,
        client.stream(
            "GET", url, headers=headers, params={"model": model} if model is not None else None
        ) as response,
    ):
        if optional and response.status_code in {404, 405, 501}:
            return None
        return read_provider_response(response, maximum=JSON_RESPONSE_LIMIT)


@lru_cache(maxsize=16)
def _public_document(url: str, timeout: float, dns_timeout: float, _window: int) -> str:
    data = _read_catalog(url, timeout, dns_timeout)
    assert data is not None
    return data.decode("utf-8")


def _document(url: str, timeout: float, dns_timeout: float) -> str:
    return _public_document(url, timeout, dns_timeout, int(monotonic() // _DOC_CACHE_SECONDS))


def _compatible_catalog(
    base_url: str, model: str, key: str, timeout: float, dns_timeout: float
) -> VoiceOptions:
    data = _read_catalog(
        base_url + "/audio/voice-catalog", timeout, dns_timeout, key=key, model=model, optional=True
    )
    if data is None:
        return ()
    body = json.loads(data)
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, dict):
        raise GatewayFailure("INVALID_RESPONSE")
    options = models.get(model, [])
    if not isinstance(options, list):
        raise GatewayFailure("INVALID_RESPONSE")
    return tuple(AudioVoiceOption.model_validate(option) for option in options)


def _siliconflow_catalog(
    base_url: str, model: str, key: str, timeout: float, dns_timeout: float
) -> VoiceOptions:
    # The native endpoint lists account reference voices across models.
    data = _read_catalog(base_url + "/audio/voice/list", timeout, dns_timeout, key=key)
    assert data is not None
    body = json.loads(data)
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        raise GatewayFailure("INVALID_RESPONSE")
    custom = []
    for entry in results:
        if not isinstance(entry, dict) or not isinstance(entry.get("model"), str):
            raise GatewayFailure("INVALID_RESPONSE")
        if entry["model"] != model:
            continue
        voice_id = entry.get("uri")
        label = entry.get("customName")
        if (
            not isinstance(voice_id, str)
            or not voice_id.startswith("speech:")
            or not isinstance(label, str)
        ):
            raise GatewayFailure("INVALID_RESPONSE")
        custom.append(AudioVoiceOption(id=voice_id, label=label))
    official = siliconflow_document_voices(
        _document(SILICONFLOW_VOICE_DOCUMENT, timeout, dns_timeout), model
    )
    return official + tuple(custom)


def _document_catalog(
    url: str,
    parse: Callable[[str, str], VoiceOptions],
    model: str,
    _key: str,
    timeout: float,
    dns_timeout: float,
) -> VoiceOptions:
    return parse(_document(url, timeout, dns_timeout), model)


_DOCUMENT_ROUTES = {
    ("openai_tts", "https://api.openai.com/v1"): (OPENAI_VOICE_DOCUMENT, openai_document_voices),
    ("qwen_tts", "https://dashscope.aliyuncs.com/api/v1"): (
        QWEN_VOICE_DOCUMENT,
        qwen_document_voices,
    ),
}


def voice_catalog_query(configuration: dict) -> VoiceCatalogQuery:
    protocol, base_url, model = (
        configuration["protocol"],
        configuration["base_url"],
        configuration["model"],
    )
    document = _DOCUMENT_ROUTES.get((protocol, base_url))
    if document is not None:
        return partial(_document_catalog, *document, model)
    # The documented workspace endpoint declares a region for the system catalog.
    if protocol == "qwen_audio_tts" and (workspace := _WORKSPACE_ROUTE.fullmatch(base_url)):
        parse = partial(qwen_audio_document_voices, region=workspace[1])
        return partial(_document_catalog, QWEN_AUDIO_VOICE_DOCUMENT, parse, model)
    if (protocol, base_url) == ("siliconflow_tts", "https://api.siliconflow.cn/v1"):
        return partial(_siliconflow_catalog, base_url, model)
    return partial(_compatible_catalog, base_url, model)
