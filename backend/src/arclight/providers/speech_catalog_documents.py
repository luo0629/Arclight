"""Interpret published voice catalogs at the provider boundary."""

import re
from html import unescape

from arclight.domain.audio_voices import AudioVoiceOption
from arclight.domain.model_gateway import GatewayFailure

OPENAI_VOICE_DOCUMENT = "https://developers.openai.com/api/docs/guides/text-to-speech.md"
QWEN_VOICE_DOCUMENT = "https://help.aliyun.com/zh/model-studio/qwen-tts-voice-list.md"
QWEN_AUDIO_VOICE_DOCUMENT = "https://help.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list.md"
SILICONFLOW_VOICE_DOCUMENT = (
    "https://docs.siliconflow.cn/docs/userguide/capabilities/text-to-speech"
)

VoiceOptions = tuple[AudioVoiceOption, ...]

_MARKDOWN_HEADING = re.compile(r"^(#{1,6}) ([^\r\n]+)", re.MULTILINE)
_ALI_FIELD = re.compile(
    r"(?:\*\*(名称|音色名称|voice ?参数|描述|特质|性别|语言)\*\*"
    r"|<strong>(名称|音色名称|voice ?参数|描述|特质|性别|语言)</strong>|(性别|语言))"
    r"[:：][ \t]*([^\r\n]+)"
)
_ALI_ROWS = re.compile(r'<Tab title="([^"]+)">|(</Tab>)|<tr\b[^>]*>(.*?)</tr>', re.DOTALL)


def _section(document: str, title: str, level: int) -> str | None:
    headings = list(_MARKDOWN_HEADING.finditer(document))
    for index, heading in enumerate(headings):
        label = heading[2].split(" <span", 1)[0].strip()
        if len(heading[1]) == level and label == title:
            end = next(
                (item.start() for item in headings[index + 1 :] if len(item[1]) <= level),
                len(document),
            )
            return document[heading.end() : end]
    return None


def openai_document_voices(document: str, model: str) -> VoiceOptions:
    section = _section(document, "Voice options", 3)
    if section is None:
        raise GatewayFailure("INVALID_RESPONSE")
    voices = re.findall(r"^-\s+\x60([a-z0-9_-]+)\x60\s*$", section, re.MULTILINE)
    if not voices:
        raise GatewayFailure("INVALID_RESPONSE")
    restricted = re.search(r"\bThe (.+?) models? support a smaller set: ([^\r\n]+)", section)
    if restricted and model in re.findall(r"\x60([^\x60]+)\x60", restricted[1]):
        subset = re.findall(r"\x60([^\x60]+)\x60", restricted[2])
        if not subset or not set(subset) <= set(voices):
            raise GatewayFailure("INVALID_RESPONSE")
        voices = subset
    elif model not in re.findall(
        r"based on (?:our |the )?\[[^\]]+\]\([^)]*/models/([^/)]+)\)", document
    ):
        return ()
    return tuple(AudioVoiceOption(id=voice, label=voice.title()) for voice in voices)


def _ali_voice(row: str) -> AudioVoiceOption | None:
    fields = {
        (bold or strong or plain).replace(" ", "").removeprefix("音色"): unescape(
            re.sub(r"<[^>]+>", "", value).strip().strip("\x60").replace(r"\_", "_")
        )
        for bold, strong, plain, value in _ALI_FIELD.findall(re.sub(r"</?p\b[^>]*>", "\n", row))
    }
    if "voice参数" not in fields:
        return None
    if "名称" not in fields:
        raise GatewayFailure("INVALID_RESPONSE")
    details = [fields[name] for name in ("描述", "特质", "性别", "语言") if fields.get(name)]
    return AudioVoiceOption(
        id=fields["voice参数"],
        label=fields["名称"],
        description="，".join(details)[:240] or None,
    )


def qwen_document_voices(document: str, model: str) -> VoiceOptions:
    section = _section(document, "非实时语音合成", 2)
    if section is None:
        raise GatewayFailure("INVALID_RESPONSE")
    supported = re.findall(
        r"^以下音色支持\s*\x60?([A-Za-z0-9_.-]+)\x60?\s*模型", section, re.MULTILINE
    )
    if not supported:
        raise GatewayFailure("INVALID_RESPONSE")
    if model not in supported:
        return ()
    return _ali_options(section)


def _ali_options(section: str, categories: set[str] | None = None) -> VoiceOptions:
    options: list[AudioVoiceOption] = []
    tabs: list[str] = []
    for opening, closing, row in _ALI_ROWS.findall(section):
        if opening:
            tabs.append(opening)
        elif closing:
            if not tabs:
                raise GatewayFailure("INVALID_RESPONSE")
            tabs.pop()
        elif categories is None or (tabs and tabs[0] in categories):
            voice = _ali_voice(row)
            if voice is not None:
                options.append(voice)
    if tabs or not options:
        raise GatewayFailure("INVALID_RESPONSE")
    return tuple(options)


def qwen_audio_document_voices(document: str, model: str, region: str) -> VoiceOptions:
    system_voices = _section(document, "系统音色", 2)
    if system_voices is None:
        raise GatewayFailure("INVALID_RESPONSE")
    section = _section(system_voices, model, 3)
    if section is None:
        return ()
    if region == "ap-southeast-1" and (
        regional_notice := re.search(r"([^<>\r\n]+)支持北京和新加坡地域", section)
    ):
        categories = set(re.split(r"[、，,]", regional_notice[1].strip()))
        return _ali_options(section, categories)
    return _ali_options(section)


def siliconflow_document_voices(document: str, model: str) -> VoiceOptions:
    start = re.search(r'<h3\b[^>]*id="21-系统预置音色"[^>]*>', document)
    if start is None:
        raise GatewayFailure("INVALID_RESPONSE")
    end = document.find("<h3", start.end())
    if end == -1:
        raise GatewayFailure("INVALID_RESPONSE")
    section = document[start.end() : end]
    supported_models = set(
        re.findall(r"<h3\b[^>]*>\s*(?:<a\b[^>]*>)?3\.\d+\s+([\w.-]+/[\w.-]+)(?=[\s<])", document)
    )
    if model not in supported_models:
        return ()
    rows = re.findall(r"<li>\s*([^<>]+?)[:：]\s*<a\b[^>]*>([\w-]+)</a>", section)
    if not rows:
        raise GatewayFailure("INVALID_RESPONSE")
    return tuple(
        AudioVoiceOption(id=f"{model}:{voice}", label=voice.title(), description=unescape(label))
        for label, voice in rows
    )
