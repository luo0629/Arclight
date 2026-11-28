"""Synthetic upstream responses for offline audio and short-drama tests."""

import httpx

from arclight.providers.speech_catalog_documents import (
    OPENAI_VOICE_DOCUMENT,
    QWEN_VOICE_DOCUMENT,
    SILICONFLOW_VOICE_DOCUMENT,
)

OPENAI_CATALOG = """# Speech

Speech is based on our [new speech model](https://docs.example/models/gpt-4o-mini-tts).

### Voice options

- \x60alloy\x60
- \x60ash\x60
- \x60coral\x60
- \x60echo\x60
- \x60fable\x60
- \x60nova\x60
- \x60onyx\x60
- \x60sage\x60
- \x60shimmer\x60
- \x60marin\x60

""" + (
    "The `tts-1` and `tts-1-hd` models support a smaller set: "
    "`alloy`, `ash`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, and `shimmer`.\n"
)

QWEN_CATALOG = """# 合成

## 非实时语音合成

以下音色支持qwen3-tts-flash模型。

<tr><td><strong>名称</strong>：芊悦<p><strong>voice参数</strong>：Cherry</p></td></tr>
"""

SILICONFLOW_CATALOG = """<h3 id="21-系统预置音色">系统预置音色</h3>
<ul><li>温和男声: <a href="https://audio.example/alex">alex</a></li></ul>
<h3 id="22-用户预置音色">用户预置音色</h3>
<h3>3.1 FunAudioLLM/CosyVoice2-0.5B 系列模型</h3>
<h3><a data-card="">3.2 fnlp/MOSS-TTSD-v0.5</a></h3>"""

DEFAULT_VOICES = (
    "test-voice",
    "another-voice",
    "new-default",
    "new-voice",
    "alloy",
    "nova",
    "Cherry",
    "model:anna",
    "longanlingxi",
    "longanhuan_v3.1",
    "v",
    "hoarse-test-voice",
    "wrong-role",
    "wrong-speaker",
)


def json_catalog(model: str, *voices: str) -> httpx.Response:
    return httpx.Response(
        200, json={"models": {model: [{"id": voice, "label": voice} for voice in voices]}}
    )


def catalog_response(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    documents = {
        OPENAI_VOICE_DOCUMENT: OPENAI_CATALOG,
        QWEN_VOICE_DOCUMENT: QWEN_CATALOG,
        SILICONFLOW_VOICE_DOCUMENT: SILICONFLOW_CATALOG,
    }
    document = documents.get(str(request.url))
    if document is not None:
        assert "Authorization" not in request.headers
        return httpx.Response(200, text=document)
    if request.url.host == "native.example" and request.url.path == "/v1/audio/voice-catalog":
        assert request.headers["Authorization"].startswith("Bearer ")
        model = request.url.params["model"]
        if model in {"FunAudioLLM/CosyVoice2-0.5B", "fnlp/MOSS-TTSD-v0.5"}:
            return json_catalog(model, f"{model}:alex")
        return json_catalog(model, *DEFAULT_VOICES)
    if request.url.host == "api.siliconflow.cn" and request.url.path == "/v1/audio/voice/list":
        return httpx.Response(200, json={"results": []})
    return httpx.Response(404)
