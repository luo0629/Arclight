"""Deployment-owned exact model declarations; personal forms never supply prices."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from arclight.domain.agent_model import AgentModelCapabilities


class ServiceModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["text", "image", "video", "speech"]
    supplier: str = Field(min_length=1, max_length=40)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=160)
    protocol: str = Field(min_length=1, max_length=40)
    agent_capabilities: AgentModelCapabilities | None = None
    maximum_cost_per_output: Decimal | None = Field(
        default=None, gt=0, le=1000000, decimal_places=6
    )
    pricing_version: str = Field(default="", max_length=100)


def matching_policy(
    policies: list[ServiceModelPolicy], configuration: dict
) -> ServiceModelPolicy | None:
    return next(
        (
            policy
            for policy in policies
            if all(
                (
                    getattr(policy, field).rstrip("/")
                    if field == "base_url"
                    else getattr(policy, field)
                )
                == (
                    str(configuration.get(field, "")).rstrip("/")
                    if field == "base_url"
                    else configuration.get(field)
                )
                for field in ("kind", "supplier", "base_url", "model", "protocol")
            )
        ),
        None,
    )
