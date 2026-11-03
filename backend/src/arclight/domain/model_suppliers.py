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
    response_format: Literal["json_schema", "json_object"] = "json_object"


# Connection defaults only: the user supplies the model ID and personal credential.
# Media presets describe native APIs; saving one does not enable a media adapter.
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
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
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
        description="Gemini · OpenAI 兼容接口",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    ),
    Supplier(
        id="zhipu",
        kind="image",
        name="智谱 CogView",
        group="domestic",
        description="智谱原生 API · 图像生成",
        base_url="https://open.bigmodel.cn/api/paas/v4",
    ),
    Supplier(
        id="volcengine",
        kind="image",
        name="火山方舟 Seedream",
        group="domestic",
        description="方舟原生 API · 图像生成",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
    ),
    Supplier(
        id="siliconflow",
        kind="image",
        name="硅基流动 SiliconFlow",
        group="domestic",
        description="硅基流动 API · 图像生成",
        base_url="https://api.siliconflow.cn/v1",
    ),
    Supplier(
        id="openai",
        kind="image",
        name="OpenAI Images",
        group="international",
        description="OpenAI 原生 API · 图像生成",
        base_url="https://api.openai.com/v1",
    ),
    Supplier(
        id="google",
        kind="image",
        name="Google Gemini",
        group="international",
        description="Gemini 原生 API · 图像生成",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    ),
    Supplier(
        id="volcengine",
        kind="video",
        name="火山方舟 Seedance",
        group="domestic",
        description="豆包视频生成 · 方舟原生 API",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
    ),
    Supplier(
        id="zhipu",
        kind="video",
        name="智谱 CogVideoX",
        group="domestic",
        description="智谱原生 API · 文生 / 图生视频",
        base_url="https://open.bigmodel.cn/api/paas/v4",
    ),
    Supplier(
        id="qwen",
        kind="video",
        name="通义万相 Wan",
        group="domestic",
        description="阿里云百炼 · 万相视频",
        base_url="https://dashscope.aliyuncs.com/api/v1",
    ),
    Supplier(
        id="google",
        kind="video",
        name="Google Veo",
        group="international",
        description="Gemini 原生 API · 视频生成",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    ),
    Supplier(
        id="xai",
        kind="video",
        name="xAI Grok Imagine",
        group="international",
        description="xAI 原生 API · 视频生成",
        base_url="https://api.x.ai/v1",
    ),
    Supplier(
        id="qwen",
        kind="speech",
        name="通义语音 Qwen TTS",
        group="domestic",
        description="阿里云百炼 · 语音合成",
        base_url="https://dashscope.aliyuncs.com/api/v1",
    ),
    Supplier(
        id="siliconflow",
        kind="speech",
        name="硅基流动 SiliconFlow",
        group="domestic",
        description="硅基流动 API · 语音合成",
        base_url="https://api.siliconflow.cn/v1",
    ),
    Supplier(
        id="openai",
        kind="speech",
        name="OpenAI Audio",
        group="international",
        description="OpenAI 原生 API · 语音合成",
        base_url="https://api.openai.com/v1",
    ),
)


def supplier_by_id(identifier: str, kind: str) -> Supplier | None:
    return next((row for row in SUPPLIERS if row.id == identifier and row.kind == kind), None)
