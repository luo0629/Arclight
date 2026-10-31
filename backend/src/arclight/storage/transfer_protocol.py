"""Small private API contract shared with the credential-free transfer process."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TransferGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "PUT"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    byte_size: int = Field(gt=0)
