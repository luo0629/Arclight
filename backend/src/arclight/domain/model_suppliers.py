"""Versioned connection presets; selection never implies a successful paid integration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Supplier(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    base_url: str
    model: str
    response_format: Literal["json_schema", "json_object"]


# Official API documentation checked 2026-11-03. Model IDs remain user-editable.
SUPPLIERS = (
    Supplier(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        model="gpt-5.4-mini",
        response_format="json_schema",
    ),
    Supplier(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        response_format="json_object",
    ),
    Supplier(
        id="zhipu",
        name="智谱 BigModel",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-5.3-flash",
        response_format="json_object",
    ),
)


def supplier_by_id(identifier: str) -> Supplier | None:
    return next((row for row in SUPPLIERS if row.id == identifier), None)
