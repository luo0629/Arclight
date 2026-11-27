"""Verify account model capabilities using only a synthetic conversation."""

from io import BytesIO
from random import SystemRandom
from secrets import randbelow
from time import monotonic
from uuid import UUID, uuid4

from PIL import Image

from arclight.core.config import Settings
from arclight.domain.agent_model import (
    AgentAssistantMessage,
    AgentModelCapabilities,
    AgentModelRequest,
    AgentModelResponse,
    AgentToolDeclaration,
    AgentToolMessage,
    AgentUserMessage,
    ContextImage,
    ResponseFormat,
)
from arclight.domain.agent_tools import AgentToolContract, AgentToolFeedback
from arclight.domain.model_gateway import GatewayFailure
from arclight.providers.agent_model import agent_transport
from arclight.providers.llm import dispatch_inference

_FORMATS: tuple[ResponseFormat, ...] = ("json_schema", "json_object")
_VERSION = "account-capability-probe/1"
_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "magenta": (255, 0, 255),
    "cyan": (0, 255, 255),
}
_TOOL = AgentToolDeclaration(
    name="probe_receipt",
    description="Return a synthetic receipt for the account model capability check.",
    parameters={
        "type": "object",
        "properties": {
            "colors": {
                "type": "array",
                "items": {"type": "string", "enum": list(_COLORS)},
            }
        },
        "required": ["colors"],
        "additionalProperties": False,
    },
)


class ProbeResult(AgentToolContract):
    value: int


def _turn(
    settings: Settings,
    credential_identity: tuple[str, ...],
    request: AgentModelRequest,
    images: dict[UUID, ContextImage],
    deadline: float,
    tools: tuple[AgentToolDeclaration, ...] = (_TOOL,),
) -> AgentModelResponse[ProbeResult] | None:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise GatewayFailure("PROVIDER_UNAVAILABLE")
    turn_settings = settings.model_copy(update={"llm_timeout_seconds": remaining})
    transport = agent_transport(turn_settings)
    if transport is None:
        return None
    provider = transport(turn_settings)
    try:
        response, _ = dispatch_inference(
            turn_settings,
            credential_identity,
            lambda: provider.complete(request, tools, ProbeResult, images),
        )
    except GatewayFailure as exc:
        if (exc.code == "INPUT_INVALID" and exc.not_accepted) or (
            exc.code == "INVALID_RESPONSE" and exc.completed
        ):
            return None
        raise
    return response


def _image_tiles(colors: list[str]) -> dict[UUID, ContextImage]:
    images = {}
    for color in colors:
        identity = uuid4()
        with Image.new("RGB", (64, 64), _COLORS[color]) as tile, BytesIO() as stream:
            tile.save(stream, format="PNG")
            images[identity] = ContextImage(identity, "image/png", stream.getvalue())
    return images


def _request(instruction: str, message: AgentUserMessage) -> AgentModelRequest:
    return AgentModelRequest(
        run_id=uuid4(),
        agent_name="producer",
        stage="capability_probe",
        operation_key="account-capability-probe",
        agent_version=_VERSION,
        prompt_version=_VERSION,
        result_schema_version=_VERSION,
        system_instruction="This is a synthetic provider capability check. " + instruction,
        messages=[message],
    )


def _challenge(
    settings: Settings,
    credential_identity: tuple[str, ...],
    deadline: float,
    capabilities: AgentModelCapabilities,
    *,
    visual: bool = False,
) -> bool:
    colors = SystemRandom().sample(list(_COLORS), k=len(_COLORS)) if visual else []
    images = _image_tiles(colors)
    probe_settings = settings.model_copy(update={"llm_agent_capabilities": capabilities})
    image_instruction = (
        "Read every attached solid-color tile in order, using the permitted English color names."
        if visual
        else "There are no tiles, so pass an empty colors list."
    )
    request = _request(
        "First call probe_receipt exactly once using its colors argument. "
        f"{image_instruction} "
        "Once the tool responds, return the integer in its result.value "
        "as a JSON object with the field value. Do not call the tool again.",
        AgentUserMessage(data={"probe": True}, image_file_ids=list(images)),
    )

    response = _turn(probe_settings, credential_identity, request, images, deadline)
    if response is None or response.status != "TOOL_CALLS" or len(response.tool_calls) != 1:
        return False
    call = response.tool_calls[0]
    if call.tool_name != _TOOL.name or call.arguments != {"colors": colors}:
        return False

    # The random value exists only in our synthetic tool result. No tool is executed.
    value = 100_000 + randbelow(900_000)
    feedback = AgentToolFeedback(
        run_id=request.run_id,
        tool_name=call.tool_name,
        idempotency_key=call.call_id,
        status="SUCCEEDED",
        result={"value": value},
    )
    follow_up = request.model_copy(
        update={
            "messages": [
                *request.messages,
                AgentAssistantMessage(
                    tool_calls=[call],
                    provider_context=response.provider_context,
                ),
                AgentToolMessage(call_id=call.call_id, feedback=feedback),
            ]
        }
    )
    final = _turn(probe_settings, credential_identity, follow_up, images, deadline)
    return final is not None and final.status == "FINAL" and final.final == ProbeResult(value=value)


def _plain_challenge(
    settings: Settings, credential_identity: tuple[str, ...], deadline: float
) -> bool:
    value = 100_000 + randbelow(900_000)
    request = _request(
        "Return the integer in data.value as a JSON object with the field value.",
        AgentUserMessage(data={"value": value}),
    )
    response = _turn(settings, credential_identity, request, {}, deadline, tools=())
    return (
        response is not None
        and response.status == "FINAL"
        and response.final == ProbeResult(value=value)
    )


def probe_model_capabilities(
    settings: Settings, credential_identity: tuple[str, ...]
) -> AgentModelCapabilities | None:
    """Check strict output, tools, and eight synthetic input images where supported."""
    deadline = monotonic() + settings.llm_timeout_seconds
    formats: tuple[ResponseFormat, ...] = (
        ("prompt_json",) if settings.llm_response_format == "prompt_json" else _FORMATS
    )
    for check_tools in (True, False):
        for response_format in formats:
            textual = AgentModelCapabilities(
                version=_VERSION,
                supplier=settings.llm_supplier,
                model=settings.llm_model,
                tools=check_tools,
                response_formats=frozenset({response_format}),
            )
            probe_settings = settings.model_copy(
                update={"llm_response_format": response_format, "llm_agent_capabilities": textual}
            )
            if not check_tools:
                if _plain_challenge(probe_settings, credential_identity, deadline):
                    return textual
                continue
            if not _challenge(probe_settings, credential_identity, deadline, textual):
                continue
            visual = textual.model_copy(update={"max_input_images": len(_COLORS)})
            if _challenge(probe_settings, credential_identity, deadline, visual, visual=True):
                return visual
            return textual
    return None
