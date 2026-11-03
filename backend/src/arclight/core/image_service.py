"""Opt-in server image configuration. Secrets never form part of its public version."""

from decimal import Decimal
from pathlib import Path
from typing import Final, Literal, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from arclight.core.errors import DomainError
from arclight.domain.versions import digest
from arclight.providers.network import validate_endpoint

OPENAI_IMAGE_MODEL: Final = "gpt-image-2.5-flare-2026-09-08"
ImageModel = Literal["gpt-image-2.5-flare-2026-09-08", "gpt-image-2.5-sunburst"]
OPENAI_IMAGE_MODELS: Final = frozenset(get_args(ImageModel))


class OpenAIImageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    enabled: bool = False
    model: ImageModel = OPENAI_IMAGE_MODEL
    base_url: str = Field(default="https://api.openai.com/v1", min_length=1, max_length=2048)
    api_key: SecretStr = SecretStr("")
    api_key_file: Path | None = None
    maximum_cost_per_image: Decimal | None = Field(default=None, gt=0, le=10000, decimal_places=6)
    pricing_version: str = Field(default="unconfigured", min_length=1, max_length=100)
    timeout_seconds: float = Field(default=120, gt=0, le=600)
    dns_timeout_seconds: float = Field(default=5, gt=0, le=30)

    @field_validator("base_url")
    @classmethod
    def normalize_endpoint(cls, value: str) -> str:
        try:
            endpoint = validate_endpoint(value)
        except DomainError:
            raise ValueError("Image service requires a public HTTPS endpoint") from None
        # A root service URL uses the Images API's standard versioned prefix.
        return endpoint if urlsplit(endpoint).path else endpoint + "/v1"

    @model_validator(mode="after")
    def distinct_secret_sources(self):
        if self.api_key_file and self.api_key.get_secret_value():
            raise ValueError("Image credentials require one secret source")
        return self

    def secret(self) -> str:
        try:
            value = (
                self.api_key_file.read_text(encoding="utf-8").strip()
                if self.api_key_file
                else self.api_key.get_secret_value()
            )
            if not value or len(value) > 2000 or any(char.isspace() for char in value):
                raise ValueError
            return value
        except (OSError, UnicodeError, ValueError):
            raise DomainError(
                "IMAGE_SERVICE_KEY_UNAVAILABLE", "图像服务密钥缺失或不可读取", 409
            ) from None

    def public_configuration(self) -> dict:
        value = {
            "provider": "openai",
            "model": self.model,
            "base_url": self.base_url,
            "maximum_cost_per_image": str(self.maximum_cost_per_image)
            if self.maximum_cost_per_image is not None
            else None,
            "pricing_version": self.pricing_version,
            "timeout_seconds": self.timeout_seconds,
            "adapter_version": "openai-images:1",
        }
        return {**value, "configuration_version": "openai-images:" + digest(value)}

    def require_ready(self) -> None:
        if not self.enabled:
            raise DomainError("IMAGE_SERVICE_DISABLED", "服务器图像服务尚未启用", 409)
        if self.maximum_cost_per_image is None or self.pricing_version == "unconfigured":
            raise DomainError("BUDGET_QUOTE_UNAVAILABLE", "图像服务缺少经核验的预算报价", 422)
        self.secret()
