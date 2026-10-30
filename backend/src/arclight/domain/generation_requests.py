"""Frozen media input contracts. Credentials and binary media are never task inputs."""

import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from arclight.domain.dependencies import DependencyInput

TargetType = Literal[
    "CHARACTER", "SCENE", "PROP", "STYLE", "SHOT_IMAGE", "AUDIO", "VIDEO", "RENDER"
]
TaskType = Literal["IMAGE", "AUDIO", "VIDEO", "RENDER"]


class MediaInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def safe_metadata(value: JsonValue) -> None:
    """Allow bounded business JSON, excluding transport credentials and inline media."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in {
                "apikey",
                "key",
                "authorization",
                "auth",
                "headers",
                "password",
                "secret",
                "token",
                "accesstoken",
                "refreshtoken",
                "clientsecret",
                "credentials",
                "cookie",
                "cookies",
                "contentbase64",
                "base64",
                "privatekey",
            } or any(
                part in normalized
                for part in ("credential", "base64", "apikey", "secret", "password")
            ):
                raise ValueError("媒体任务输入不能包含凭证或内联媒体")
            safe_metadata(item)
    elif isinstance(value, list):
        for item in value:
            safe_metadata(item)
    elif isinstance(value, str) and re.search(
        r"(?i)(?:bearer\s+\S|https?://[^/\s]*@|[?&](?:api[_-]?key|token|signature|secret|x-amz-[a-z-]+)=|data:[^;]*;base64,|\bsk-[a-z0-9_-]{12,})",
        value,
    ):
        raise ValueError("媒体任务输入不能包含凭证或内联媒体")


class GenerationSpec(MediaInput):
    schema_version: Literal[1] = 1
    provider: Literal["mock"] = "mock"
    model: str = Field(min_length=1, max_length=160)
    model_config_version: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=20000)
    prompt_version: str = Field(min_length=1, max_length=100)
    negative_prompt: str | None = Field(default=None, max_length=10000)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    output_spec: dict[str, JsonValue]
    context: dict[str, JsonValue] = Field(default_factory=dict)
    seed: int | None = Field(default=None, strict=True, ge=0, le=9223372036854775807)
    variation: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def bounded_business_snapshot(self):
        safe_metadata(self.model_dump(mode="json"))
        if "seed" in self.parameters or "variation" in self.parameters:
            raise ValueError("随机探索请使用显式seed与variation字段")
        if any(
            not value.strip()
            for value in (
                self.model,
                self.model_config_version,
                self.prompt,
                self.prompt_version,
            )
        ):
            raise ValueError("模型、提示词及版本不能为空白")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode()) > 65536:
            raise ValueError("媒体输入快照超出大小限制")
        return self


class ExpectedAsset(MediaInput):
    revision: int = Field(strict=True, ge=0)
    lock_version: int = Field(strict=True, ge=0)
    current_version_id: UUID | None


class GenerationCreate(MediaInput):
    episode_id: UUID | None = None
    target_type: TargetType
    target_asset_id: UUID | None = None
    expected_asset: ExpectedAsset | None = None
    task_type: TaskType
    inputs: list[DependencyInput] = Field(default_factory=list, max_length=64)
    spec: GenerationSpec

    @model_validator(mode="after")
    def validate_target(self):
        expected_type = (
            self.target_type if self.target_type in {"AUDIO", "VIDEO", "RENDER"} else "IMAGE"
        )
        if self.task_type != expected_type:
            raise ValueError("目标与任务类型不一致")
        if (self.target_asset_id is None) != (self.expected_asset is None):
            raise ValueError("资产目标必须同时提供精确当前版本与修订快照")
        identities = [(item.source_asset_version_id, item.dependency_type) for item in self.inputs]
        if len(identities) != len(set(identities)):
            raise ValueError("精确输入版本与类型不能重复")
        return self


class GenerationResult(MediaInput):
    """Normalized provider receipt; this does not register or approve candidate assets."""

    schema_version: Literal[1] = 1
    output_handles: list[str] = Field(min_length=1, max_length=64)
    usage: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe_result(self):
        if any(
            not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", item) or "://" in item
            for item in self.output_handles
        ):
            raise ValueError("结果仅接受有界供应商对象标识，不保存临时URL")
        safe_metadata(self.usage)
        safe_metadata(self.metadata)
        if len(json.dumps(self.model_dump(mode="json")).encode()) > 65536:
            raise ValueError("媒体结果回执超出大小限制")
        return self


MediaErrorCode = Literal[
    "INPUT_INVALID",
    "PROVIDER_REJECTED",
    "PROVIDER_FAILED",
    "PROVIDER_UNAVAILABLE",
    "RESULT_UNKNOWN",
    "STORAGE_FAILED",
    "CANCELLED",
    "BUDGET_EXCEEDED",
    "QUALITY_FAILED",
]
