"""Versioned connection presets; selection never implies a successful paid integration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Supplier(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    kind: Literal["text", "image", "video", "speech"]
    name: str
    group: Literal["domestic", "international"]
    description: str
    base_url: str
    protocol: str = "openai_chat"
    response_format: Literal["json_schema", "json_object"] = "json_object"


class SpeechModelPreset(BaseModel):
    """Optional model suggestions; never defaults, admission rules or price quotes."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    supplier: str
    model: str
    name: str
    description: str


# Verified against official API documentation on 2026-11-12.
# A suggestion does not expand the installed protocol's parameters or audio behavior.
SPEECH_MODEL_PRESETS = (
    SpeechModelPreset(
        supplier="qwen",
        model="qwen3-tts-flash",
        name="Qwen3 TTS Flash",
        description=(
            "千问非实时语音合成；当前官方预设使用中国内地接口与对应 API Key；"
            "使用 Qwen3 音色和原生 WAV，可设置 language_type。"
        ),
    ),
    SpeechModelPreset(
        supplier="siliconflow",
        model="FunAudioLLM/CosyVoice2-0.5B",
        name="CosyVoice2 0.5B",
        description=(
            "支持中文、英文及多语种；voice 需带模型前缀，如 FunAudioLLM/CosyVoice2-0.5B:alex。"
        ),
    ),
    SpeechModelPreset(
        supplier="siliconflow",
        model="fnlp/MOSS-TTSD-v0.5",
        name="MOSS TTSD v0.5",
        description=(
            "中英长文本语音；当前按单角色单音色使用，"
            "voice 如 fnlp/MOSS-TTSD-v0.5:alex；建议 WAV 或 MP3。"
        ),
    ),
)


# Connection defaults only: the user supplies the model ID and personal credential.
# Protocols are explicit: a vendor name is not evidence of model capabilities.
SUPPLIERS = (
    Supplier(
        id="openai",
        kind="text",
        name="OpenAI",
        group="international",
        description="OpenAI · 文字生成",
        base_url="https://api.openai.com/v1",
        response_format="json_schema",
    ),
    Supplier(
        id="deepseek",
        kind="text",
        name="DeepSeek",
        group="domestic",
        description="DeepSeek · 文字生成",
        base_url="https://api.deepseek.com",
    ),
    Supplier(
        id="zhipu",
        kind="text",
        name="智谱 BigModel",
        group="domestic",
        description="GLM · 文字生成",
        base_url="https://open.bigmodel.cn/api/paas/v4",
    ),
    Supplier(
        id="qwen",
        kind="text",
        name="通义千问",
        group="domestic",
        description="阿里云百炼 · 文字生成",
        base_url="https://dashscope.aliyuncs.com/api/v1",
        protocol="qwen_chat",
    ),
    Supplier(
        id="siliconflow",
        kind="text",
        name="硅基流动 SiliconFlow",
        group="domestic",
        description="OpenAI 兼容 · 文字生成",
        base_url="https://api.siliconflow.cn/v1",
    ),
    Supplier(
        id="google",
        kind="text",
        name="Google Gemini",
        group="international",
        description="Gemini 原生 API · 文字生成",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        protocol="gemini",
    ),
    Supplier(
        id="zhipu",
        kind="image",
        name="智谱 CogView",
        group="domestic",
        description="智谱原生 API · 图像生成",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        protocol="cogview",
    ),
    Supplier(
        id="volcengine",
        kind="image",
        name="火山方舟 Seedream",
        group="domestic",
        description="方舟原生 API · 图像生成",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        protocol="seedream",
    ),
    Supplier(
        id="siliconflow",
        kind="image",
        name="硅基流动 SiliconFlow",
        group="domestic",
        description="硅基流动 API · 图像生成",
        base_url="https://api.siliconflow.cn/v1",
        protocol="siliconflow_image",
    ),
    Supplier(
        id="openai",
        kind="image",
        name="OpenAI Images",
        group="international",
        description="OpenAI 原生 API · 图像生成",
        base_url="https://api.openai.com/v1",
        protocol="openai_image",
    ),
    Supplier(
        id="google",
        kind="image",
        name="Google Gemini",
        group="international",
        description="Gemini 原生 API · 图像生成",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        protocol="gemini_image",
    ),
    Supplier(
        id="volcengine",
        kind="video",
        name="火山方舟 Seedance",
        group="domestic",
        description="豆包视频生成 · 方舟原生 API",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        protocol="seedance",
    ),
    Supplier(
        id="zhipu",
        kind="video",
        name="智谱 CogVideoX",
        group="domestic",
        description="智谱原生 API · 文生视频",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        protocol="cogvideo",
    ),
    Supplier(
        id="qwen",
        kind="video",
        name="通义万相 Wan",
        group="domestic",
        description="阿里云百炼 · 万相视频",
        base_url="https://dashscope.aliyuncs.com/api/v1",
        protocol="wan",
    ),
    Supplier(
        id="google",
        kind="video",
        name="Google Gemini 视频",
        group="international",
        description="Gemini Omni 原生 API · 视频生成",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        protocol="gemini_video",
    ),
    Supplier(
        id="xai",
        kind="video",
        name="xAI Grok Imagine",
        group="international",
        description="xAI 原生 API · 视频生成",
        base_url="https://api.x.ai/v1",
        protocol="xai_video",
    ),
    Supplier(
        id="qwen",
        kind="speech",
        name="通义语音 Qwen TTS",
        group="domestic",
        description="阿里云百炼 · 语音合成",
        base_url="https://dashscope.aliyuncs.com/api/v1",
        protocol="qwen_tts",
    ),
    Supplier(
        id="siliconflow",
        kind="speech",
        name="硅基流动 SiliconFlow",
        group="domestic",
        description="硅基流动 API · 语音合成",
        base_url="https://api.siliconflow.cn/v1",
        protocol="siliconflow_tts",
    ),
    Supplier(
        id="openai",
        kind="speech",
        name="OpenAI Audio",
        group="international",
        description="OpenAI 原生 API · 语音合成",
        base_url="https://api.openai.com/v1",
        protocol="openai_tts",
    ),
)


def supplier_by_id(identifier: str, kind: str = "text") -> Supplier | None:
    return next((row for row in SUPPLIERS if row.id == identifier and row.kind == kind), None)


# A projection of the original contract, never used to default new configurations.
LEGACY_MODELS = {
    "openai": "gpt-5.4-mini",
    "deepseek": "deepseek-flash",
    "zhipu": "glm-5.3-flash",
}
PROTOCOLS = {
    "text": ("openai_chat", "gemini", "qwen_chat"),
    "image": ("openai_image", "cogview", "seedream", "siliconflow_image", "gemini_image"),
    "video": ("seedance", "cogvideo", "wan", "gemini_video", "xai_video", "veo"),
    "speech": ("openai_tts", "siliconflow_tts", "qwen_tts"),
}
