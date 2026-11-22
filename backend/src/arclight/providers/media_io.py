"""Bound worker waiting and normalize native provider responses."""

import json
from collections.abc import Callable
from queue import Empty, Queue
from threading import BoundedSemaphore, Thread
from typing import cast

import httpx

from arclight.domain.model_gateway import GatewayFailure
from arclight.providers.chat import raise_for_provider_status

JSON_RESPONSE_LIMIT = 2 * 1024 * 1024
_CAPACITY = BoundedSemaphore(32)


def bounded_call[T](
    call: Callable[[], T], seconds: float, *, slots: BoundedSemaphore = _CAPACITY
) -> T:
    if seconds <= 0:
        raise TimeoutError("Provider call deadline reached")
    if not slots.acquire(blocking=False):
        raise TimeoutError("Provider I/O capacity is occupied")
    output: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            output.put((True, call()))
        except BaseException as exc:
            output.put((False, exc))
        finally:
            slots.release()

    # A timed-out thread retains its slot until it exits. Waiting is bounded, not the
    # external operation; durable generation capacity requires authoritative evidence.
    try:
        Thread(target=invoke, name="provider-io", daemon=True).start()
    except RuntimeError:
        slots.release()
        raise
    try:
        successful, value = output.get(timeout=seconds)
    except Empty as exc:
        raise TimeoutError("Provider response was not received before the deadline") from exc
    if not successful:
        assert isinstance(value, BaseException)
        raise value
    return cast(T, value)


def read_provider_response(response: httpx.Response, *, maximum: int | None) -> bytes:
    raw = bytearray()
    for chunk in response.iter_bytes():
        if maximum is not None and len(raw) + len(chunk) > maximum:
            raise GatewayFailure("INVALID_RESPONSE")
        raw.extend(chunk)
    try:
        error = json.loads(raw) if response.is_error else {}
    except ValueError:
        error = {}
    if isinstance(error, dict) and error.get("code") and not error.get("error"):
        error = {"error": error}
    raise_for_provider_status(response, error if isinstance(error, dict) else {})
    return bytes(raw)
