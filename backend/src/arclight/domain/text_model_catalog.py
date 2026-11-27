"""Text model suggestions and independently declared execution capabilities."""

from typing import NamedTuple

from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.model_suppliers import supplier_by_id
from arclight.domain.service_models import ServiceModelPolicy


class TextModelPreset(NamedTuple):
    supplier: str
    model: str
    name: str


_OPENAI_MINI = TextModelPreset("openai", "gpt-5.4-mini", "GPT-5.4 mini")

# Official model IDs. Selection alone grants no runtime capabilities.
TEXT_MODEL_PRESETS = (
    _OPENAI_MINI,
    TextModelPreset("deepseek", "deepseek-flash", "DeepSeek V4.1 Flash"),
    TextModelPreset("deepseek", "deepseek-v4-pro", "DeepSeek V4 Pro"),
    TextModelPreset("zhipu", "glm-5.3", "GLM-5.3"),
    TextModelPreset("qwen", "qwen-plus", "Qwen Plus"),
    TextModelPreset("siliconflow", "deepseek-ai/DeepSeek-V4-Flash", "DeepSeek V4 Flash"),
    TextModelPreset("google", "gemini-3.8-flash", "Gemini 3.8 Flash"),
    TextModelPreset("kimi", "kimi-k3.1-pro", "Kimi K3.1 Pro"),
    TextModelPreset("minimax", "MiniMax-M3.1-Flash", "MiniMax M3.1 Flash"),
    TextModelPreset("minimax", "MiniMax-M3", "MiniMax M3"),
    TextModelPreset("volcengine", "doubao-seed-2-1-pro-260915", "Doubao Seed 2.1 Pro"),
    TextModelPreset("volcengine", "doubao-seed-2-1-lite-260915", "Doubao Seed 2.1 Lite"),
    TextModelPreset("openrouter", "openai/gpt-5.4-mini", "GPT-5.4 mini"),
    TextModelPreset("mistral", "mistral-small-2603", "Mistral Small 4"),
)

_OPENAI = supplier_by_id(_OPENAI_MINI.supplier)
assert _OPENAI is not None
TEXT_MODEL_POLICIES = [
    ServiceModelPolicy(
        kind="text",
        supplier=_OPENAI.id,
        base_url=_OPENAI.base_url,
        model=_OPENAI_MINI.model,
        protocol=_OPENAI.protocol,
        agent_capabilities=AgentModelCapabilities(
            version="official-text-model/1",
            supplier=_OPENAI.id,
            model=_OPENAI_MINI.model,
            tools=True,
            response_formats=frozenset({"json_schema"}),
            max_input_images=8,
        ),
    )
]
