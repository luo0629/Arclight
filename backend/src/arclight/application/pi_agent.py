"""Private Pi execution transport. Python remains the authority for all effects."""

import json
import os
from collections import deque
from contextlib import suppress
from pathlib import Path
from queue import Empty, Full, Queue
from shutil import which
from subprocess import DEVNULL, PIPE, Popen, TimeoutExpired
from threading import Thread
from types import TracebackType
from typing import Self

from pydantic import BaseModel

from arclight.application.agent_workflows import WorkflowRecoveryFailed
from arclight.domain.agent_model import AgentModelRequest, AgentModelResponse, ModelToolCall
from arclight.domain.agent_tools import AgentToolFeedback
from arclight.domain.model_gateway import GatewayUsage

_RUNNER = Path(__file__).resolve().parents[3] / "pi_runtime" / "runner.mjs"
_MAX_FRAME_BYTES = 16 * 1024 * 1024
_EVENT_TIMEOUT = 20.0
_NODE_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")


class PiRuntimeError(WorkflowRecoveryFailed):
    """The Pi process failed before the host could advance its checkpoint."""


def _read_events(process: Popen[bytes], events: Queue[bytes]) -> None:
    assert process.stdout is not None
    try:
        while True:
            frame = process.stdout.readline(_MAX_FRAME_BYTES + 1)
            events.put_nowait(frame)
            if not frame or not frame.endswith(b"\n") or len(frame) > _MAX_FRAME_BYTES:
                break
    except (OSError, ValueError, Full):
        return


def _wire_tool(call: ModelToolCall) -> dict:
    wire = {"id": call.call_id, "name": call.tool_name, "arguments": call.arguments}
    if call.provider_signature is not None:
        wire["signature"] = call.provider_signature
    return wire


def _write_frame(process: Popen[bytes], payload: bytes, errors: list[OSError]) -> None:
    assert process.stdin is not None
    try:
        process.stdin.write(payload)
        process.stdin.flush()
    except OSError as exc:
        errors.append(exc)


class PiAgent:
    """Reattach Pi at a pending model turn or a checkpointed batch of tool calls."""

    def __init__(self, seed: AgentModelRequest) -> None:
        instructions = "\n\n".join(
            [seed.system_instruction, *(skill.instructions for skill in seed.skills)]
        )
        self._agent = {
            "name": seed.agent_name,
            "stage": seed.stage,
            "instruction": instructions,
            "input": [message.model_dump(mode="json") for message in seed.messages],
        }
        self._process: Popen[bytes] | None = None
        self._events: Queue[bytes] = Queue(maxsize=4)
        self._reader: Thread | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _start(self, resume: str, calls: list[ModelToolCall]) -> None:
        executable = which("node")
        if executable is None:
            raise PiRuntimeError("Pi runtime requires Node.js")
        try:
            process = Popen(
                [executable, str(_RUNNER)],
                stdin=PIPE,
                stdout=PIPE,
                stderr=DEVNULL,
                env={key: os.environ[key] for key in _NODE_ENV if key in os.environ},
            )
        except OSError as exc:
            raise PiRuntimeError("Could not start the Pi runtime") from exc
        self._process = process
        self._events = Queue(maxsize=4)
        self._reader = Thread(target=_read_events, args=(process, self._events), daemon=True)
        self._reader.start()
        self._send(
            {
                "type": "start",
                "resume": resume,
                "calls": [_wire_tool(call) for call in calls],
                "tools": list(dict.fromkeys(call.tool_name for call in calls)),
                "agent": self._agent,
            }
        )

    def _send(self, frame: dict) -> None:
        process = self._process
        assert process is not None and process.stdin is not None
        payload = (json.dumps(frame, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(payload) > _MAX_FRAME_BYTES:
            raise PiRuntimeError("Pi runtime request exceeds the frame limit")
        errors: list[OSError] = []
        writer = Thread(target=_write_frame, args=(process, payload, errors), daemon=True)
        writer.start()
        try:
            writer.join(timeout=_EVENT_TIMEOUT)
            if writer.is_alive():
                raise PiRuntimeError("Timed out sending a request to the Pi runtime")
        finally:
            if writer.is_alive():
                process.kill()
                writer.join()
        if errors:
            raise PiRuntimeError("Pi runtime stopped accepting requests") from errors[0]

    def _event(self, kind: str) -> dict:
        try:
            frame = self._events.get(timeout=_EVENT_TIMEOUT)
            if not frame:
                raise PiRuntimeError("Pi runtime exited before returning an event")
            if not frame.endswith(b"\n") or len(frame) > _MAX_FRAME_BYTES:
                raise PiRuntimeError("Pi runtime returned an invalid frame")
            actual = json.loads(frame)
        except Empty as exc:
            raise PiRuntimeError("Timed out waiting for the Pi runtime") from exc
        except (ValueError, RecursionError) as exc:
            raise PiRuntimeError("Pi runtime returned an invalid event") from exc
        if not isinstance(actual, dict) or actual.get("type") != kind:
            raise PiRuntimeError("Pi runtime returned an unexpected event")
        return actual

    def _expect(self, kind: str) -> None:
        if self._event(kind) != {"type": kind}:
            raise PiRuntimeError("Pi runtime returned an unexpected event")

    def request_model(self) -> None:
        if self._process is None:
            self._start("model", [])
        self._expect("model")

    def accept_model[T: BaseModel](
        self, response: AgentModelResponse[T], tools: tuple[str, ...], usage: GatewayUsage
    ) -> None:
        tokens = usage.model_dump(include={"input_tokens", "output_tokens"}, exclude_none=True)
        if response.status == "TOOL_CALLS":
            self._send(
                {
                    "type": "continue",
                    "calls": [_wire_tool(call) for call in response.tool_calls],
                    "tools": tools,
                    "usage": tokens,
                }
            )
            return
        assert response.final is not None
        self._send(
            {"type": "stop", "result": response.final.model_dump(mode="json"), "usage": tokens}
        )
        self._expect("done")
        self.close()

    def prepare_tools(self, calls: list[ModelToolCall]) -> None:
        if self._process is None:
            self._start("tools", calls)

    def next_tool(self, pending: deque[ModelToolCall]) -> ModelToolCall:
        event = self._event("tool")
        call = pending.popleft()
        if event.get("id") != call.call_id or event.get("name") != call.tool_name:
            raise PiRuntimeError("Pi tool dispatch does not match the authorized model response")
        # Use Python's validated arguments. JSON numbers may round in the JS runtime.
        return call

    def accept_tool(self, feedback: AgentToolFeedback) -> None:
        self._send({"type": "continue", "result": feedback.model_dump(mode="json")})

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        assert process.stdin is not None and process.stdout is not None
        with suppress(OSError):
            process.stdin.close()
        try:
            process.wait(timeout=0.25)
        except TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
        if self._reader is not None:
            self._reader.join()
            self._reader = None
