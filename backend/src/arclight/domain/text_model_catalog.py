"""Exact model declarations maintained against the official protocol documentation."""

from typing import Literal

from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.model_suppliers import supplier_by_id
from arclight.domain.service_models import ServiceModelPolicy

type TextModel = tuple[str, str, str, Literal["json_schema", "json_object"], int]

TEXT_MODELS: tuple[TextModel, ...] = (("openai", "gpt-5.4-mini", "GPT-5.4 mini", "json_schema", 8),)


def _declaration(entry: TextModel) -> ServiceModelPolicy:
    supplier, model, _, response_format, max_input_images = entry
    preset = supplier_by_id(supplier)
    assert preset is not None
    return ServiceModelPolicy(
        kind="text",
        supplier=preset.id,
        base_url=preset.base_url,
        model=model,
        protocol=preset.protocol,
        agent_capabilities=AgentModelCapabilities(
            version="official-text-model/1",
            supplier=preset.id,
            model=model,
            tools=True,
            response_formats=frozenset({response_format}),
            max_input_images=max_input_images,
        ),
    )


TEXT_MODEL_POLICIES = [_declaration(model) for model in TEXT_MODELS]
