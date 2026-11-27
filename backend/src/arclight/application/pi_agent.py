"""Private Pi execution transport. Python remains the authority for all effects."""

import json
import os
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
from arclight.domain.agent_model import (
    AgentAssistantMessage,
    AgentMessage,
    AgentModelRequest,
    AgentModelResponse,
    AgentToolDeclaration,
    AgentToolMessage,
    AgentUserMessage,
    ModelToolCall,
)
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
    """Send Pi canonical history and resolve its context to trusted Python records."""

    def __init__(self, seed: AgentModelRequest) -> None:
        instructions = "\n\n".join(
            [seed.system_instruction, *(skill.instructions for skill in seed.skills)]
        )
        self._agent = {
            "name": seed.agent_name,
            "stage": seed.stage,
            "instruction": instructions,
        }
        self.turn = 0
        self._users: dict[str, AgentUserMessage] = {}
        self._assistants: dict[int, AgentAssistantMessage] = {}
        self._feedback: dict[tuple[int, str], AgentToolMessage] = {}
        self._active_tool: ModelToolCall | None = None
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

    def _remember_user(self, message: AgentUserMessage) -> str:
        value = message.model_dump_json()
        self._users[value] = message
        return value

    def start(self, messages: list[AgentMessage]) -> None:
        history: list[dict] = []
        turn = 0
        for message in messages:
            if isinstance(message, AgentUserMessage):
                history.append({"role": "user", "text": self._remember_user(message)})
            elif isinstance(message, AgentAssistantMessage):
                turn += 1
                self._assistants[turn] = message
                history.append(
                    {
                        "role": "assistant",
                        "turn": turn,
                        "calls": [_wire_tool(call) for call in message.tool_calls],
                    }
                )
            else:
                self._feedback[turn, message.call_id] = message
                history.append(
                    {
                        "role": "tool",
                        "turn": turn,
                        "id": message.call_id,
                        "name": message.feedback.tool_name,
                        "result": message.feedback.model_dump_json(),
                        "isError": message.feedback.error is not None,
                    }
                )
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
        self._send({"type": "start", "agent": self._agent, "messages": history})

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

    def event(self) -> dict:
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
        if not isinstance(actual, dict) or actual.get("type") not in (
            "prepare",
            "model",
            "tool",
            "tool_rejected",
            "done",
        ):
            raise PiRuntimeError("Pi runtime returned an unexpected event")
        return actual

    def prepare(
        self, tools: tuple[AgentToolDeclaration, ...], messages: list[AgentUserMessage]
    ) -> None:
        self._send(
            {
                "type": "continue",
                "tools": [tool.model_dump(mode="json") for tool in tools],
                "messages": [self._remember_user(message) for message in messages],
            }
        )

    def model_messages(
        self, event: dict, tools: tuple[AgentToolDeclaration, ...]
    ) -> list[AgentMessage]:
        actual_tools = event.get("tools")
        expected = {tool.name: tool.model_dump(mode="json") for tool in tools}
        frames = event.get("messages")
        if not isinstance(actual_tools, list) or not isinstance(frames, list):
            raise PiRuntimeError("Pi runtime did not return model messages and tool declarations")
        try:
            if (
                len(actual_tools) != len(expected)
                or {tool["name"]: tool for tool in actual_tools} != expected
            ):
                raise ValueError("Tool declarations differ")
            messages: list[AgentMessage] = []
            for frame in frames:
                if frame["role"] == "user":
                    messages.append(self._users[frame["text"]])
                elif frame["role"] == "assistant":
                    assistant = self._assistants[frame["turn"]]
                    references = [
                        {"id": call.call_id, "name": call.tool_name}
                        for call in assistant.tool_calls
                    ]
                    if frame["calls"] != references:
                        raise ValueError("Tool call references differ")
                    messages.append(assistant)
                elif frame["role"] == "tool":
                    message = self._feedback[frame["turn"], frame["id"]]
                    if message.feedback.tool_name != frame["name"]:
                        raise ValueError("Tool result reference differs")
                    messages.append(message)
                else:
                    raise ValueError("Unknown message role")
        except (KeyError, ValueError, TypeError) as exc:
            raise PiRuntimeError("Pi runtime returned an invalid model context") from exc
        return messages

    def accept_model[T: BaseModel](
        self, response: AgentModelResponse[T], tools: tuple[str, ...], usage: GatewayUsage
    ) -> None:
        tokens = usage.model_dump(include={"input_tokens", "output_tokens"}, exclude_none=True)
        if response.status == "TOOL_CALLS":
            if not {call.tool_name for call in response.tool_calls}.issubset(tools):
                raise PiRuntimeError("The model returned an unauthorized tool")
            self._assistants[self.turn] = AgentAssistantMessage(
                tool_calls=response.tool_calls,
                provider_context=response.provider_context,
            )
            self._send(
                {
                    "type": "continue",
                    "turn": self.turn,
                    "calls": [_wire_tool(call) for call in response.tool_calls],
                    "usage": tokens,
                }
            )
            return
        assert response.final is not None
        self._send({"type": "stop", "result": response.final.model_dump_json(), "usage": tokens})
        if self.event() != {"type": "done"}:
            raise PiRuntimeError("Pi runtime did not finish the model turn")
        self.close()

    def next_tool(self, event: dict) -> ModelToolCall:
        assistant = self._assistants.get(self.turn)
        if assistant is None or event.get("turn") != self.turn:
            raise PiRuntimeError("Pi tool dispatch has no active model response")
        pending = (
            call for call in assistant.tool_calls if (self.turn, call.call_id) not in self._feedback
        )
        call = next(pending, None)
        if call is None or event.get("id") != call.call_id or event.get("name") != call.tool_name:
            raise PiRuntimeError("Pi tool dispatch does not match the authorized model response")
        self._active_tool = call
        return call

    def accept_tool(self, feedback: AgentToolFeedback) -> None:
        call = self._active_tool
        assert call is not None
        message = AgentToolMessage(
            call_id=call.call_id,
            feedback=feedback.model_copy(update={"idempotency_key": call.call_id}),
        )
        self._feedback[self.turn, call.call_id] = message
        self._active_tool = None
        self._send(
            {
                "type": "continue",
                "result": message.feedback.model_dump_json(),
                "isError": feedback.error is not None,
            }
        )

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
