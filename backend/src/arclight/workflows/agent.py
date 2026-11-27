"""The persisted cursor for the shared Pi session."""

from typing import Literal, TypedDict


class AgentLoopState(TypedDict, total=False):
    request: dict
    observed_round: int
    model_turns: int
    response: dict | None
    outcome: dict | None
    phase: Literal["prepare", "model", "tools", "finish"]
