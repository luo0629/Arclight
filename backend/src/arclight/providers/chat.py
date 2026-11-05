"""Shared sanitized Chat Completions errors, completion state and token receipts."""

import httpx
from pydantic import ValidationError

from arclight.domain.model_gateway import GatewayFailure, GatewayUsage


def response_usage(body: dict) -> GatewayUsage:
    reported = body.get("usage")
    reported = reported if isinstance(reported, dict) else {}
    values = {}
    for source, target in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ):
        value = reported.get(source)
        try:
            GatewayUsage.model_validate({target: value})
        except ValidationError:
            continue
        values[target] = value
    # An unverified supplier amount or a token count is not a billing receipt.
    return GatewayUsage.model_validate(values)


def raise_for_provider_status(response: httpx.Response, body: dict | None = None) -> None:
    """Never expose vendor messages, request headers or response content in exceptions."""
    if response.is_redirect:
        raise GatewayFailure("SERVICE_REDIRECT_BLOCKED")
    if not response.is_error:
        return
    if body is None:
        try:
            value = response.json()
            body = value if isinstance(value, dict) else {}
        except ValueError:
            body = {}
    error = body.get("error", {})
    codes = (
        {str(error.get(field, "")).lower() for field in ("code", "type")}
        if isinstance(error, dict)
        else set()
    )
    quota_codes = {
        "insufficient_quota",
        "insufficient_balance",
        "balance_not_enough",
        "quota_exceeded",
        "billing_hard_limit_reached",
        "credit_balance_too_low",
        "1113",
    }
    status = response.status_code
    usage = response_usage(body)
    billed_work = any(value not in (None, 0) for value in usage.model_dump().values())

    def rejection(code: str) -> GatewayFailure:
        # An error with a positive usage receipt must not be declared free/non-accepted.
        return (
            GatewayFailure(code, completed=True, usage=usage)
            if billed_work
            else GatewayFailure(code, not_accepted=True)
        )

    if status == 401:
        raise rejection("PROVIDER_AUTHENTICATION_FAILED")
    if status == 402 or (status in {400, 403, 429} and codes & quota_codes):
        raise rejection("PROVIDER_QUOTA_EXHAUSTED")
    if status == 429:
        raise rejection("PROVIDER_RATE_LIMITED")
    if status == 403:
        raise rejection("PROVIDER_ACCESS_DENIED")
    if status in {400, 404, 422}:
        raise rejection("INPUT_INVALID")
    if billed_work:
        raise GatewayFailure("PROVIDER_UNAVAILABLE", completed=True, usage=usage)
    raise GatewayFailure("PROVIDER_UNAVAILABLE")


def completed_message(body: dict, *, strict: bool = True) -> tuple[dict, str]:
    usage = response_usage(body)
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise GatewayFailure("INVALID_RESPONSE", completed=True, usage=usage)
    choice = choices[0]
    message = choice.get("message")
    reason = choice.get("finish_reason", None if strict else "stop")
    if not isinstance(message, dict):
        raise GatewayFailure("INVALID_RESPONSE", completed=True, usage=usage)
    if message.get("refusal") or reason == "content_filter":
        raise GatewayFailure("PROVIDER_REFUSED", completed=True, usage=usage)
    if reason == "length":
        raise GatewayFailure("PROVIDER_TRUNCATED", completed=True, usage=usage)
    if reason not in {"stop", "tool_calls"}:
        raise GatewayFailure("INVALID_RESPONSE", completed=True, usage=usage)
    return message, reason
