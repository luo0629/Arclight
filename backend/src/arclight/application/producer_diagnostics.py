"""One exact vocabulary for public Producer progress and event diagnostics."""

from typing import get_args

from arclight.application.project_production import PUBLIC_ERROR_ALIASES
from arclight.domain.project_production import PublicError

# Exact server-owned values are safe; arbitrary provider text is never accepted.
_CONTROL_CODES = frozenset(
    {
        "NOT_FOUND",
        "FORBIDDEN",
        "USER_NOT_AUTHORIZED",
        "PROJECT_ARCHIVED",
        "EPISODE_ARCHIVED",
        "AGENT_NOT_ACTIVE",
        "AGENT_CANCELLED",
        "AGENT_HUMAN_STOPPED",
        "AGENT_DEADLINE_EXCEEDED",
        "AGENT_BUDGET_EXCEEDED",
        "AGENT_COST_LIMIT",
        "AGENT_INPUT_CHANGED",
        "AGENT_LIMITS_REQUIRED",
        "AGENT_LIMITS_INVALID",
        "AGENT_SCOPE_INVALID",
        "RUN_STATE_INVALID",
        "AGENT_STEP_LIMIT",
        "AGENT_CALL_LIMIT",
        "AGENT_MODEL_CALL_LIMIT",
        "AGENT_TOOL_CALL_LIMIT",
        "AGENT_AGENT_CALL_LIMIT",
        "AGENT_DELEGATION_LIMIT",
        "AGENT_DELEGATION_DEPTH",
        "AGENT_TOOL_GRANT_FORBIDDEN",
        "PRODUCER_INITIATOR_REQUIRED",
        "PRODUCER_COMMAND_FAILED",
        "PRODUCER_COMMAND_INVALID",
        "PRODUCER_PLAN_SUPERSEDED",
        "PRODUCER_PREREQUISITE_PENDING",
        "PRODUCER_QUESTION_PENDING",
        "PRODUCER_SCOPE_INVALID",
        "AGENT_LOOP_BUSY",
        "PRODUCER_OBSERVATION_BUSY",
        "PRODUCER_PAUSED",
        "PRODUCER_SERIES_SCOPE",
        "PRODUCER_SERIES_INPUT",
        "PRODUCER_SERIES_CONTROL",
        "PRODUCER_DELIVERY_SCOPE",
        "PRODUCER_CONTENT_APPROVAL_REQUIRED",
        "PRODUCER_FORMAL_DELIVERY_REQUIRED",
        "PRODUCER_COST_UNKNOWN",
    }
)


def public_diagnostic(code: str | None) -> str | None:
    if code is None or code in _CONTROL_CODES or code in get_args(PublicError):
        return code
    return next(
        (public for public, aliases in PUBLIC_ERROR_ALIASES.items() if code in aliases),
        "INTERNAL_ERROR",
    )
