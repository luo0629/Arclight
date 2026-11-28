"""Assign missing short-drama roles from one frozen speech catalog."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_runtime import run_persistent_agent_loop
from arclight.application.agent_tools import AgentToolRegistry
from arclight.application.audio_voice_catalog import query_voice_catalog, read_voice_catalog_source
from arclight.application.gateway_agent import AgentOutputProjection
from arclight.application.media_services import restore_selection
from arclight.application.model_gateway import lock_gate
from arclight.application.producer_interaction import COMMAND, require_producer
from arclight.application.production_versions import bind, find_binding
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentDecision, AgentLoopResult
from arclight.domain.audio_voices import AudioVoiceCatalogRead
from arclight.domain.producer_interaction import ProducerVoiceCastingCommand
from arclight.domain.versions import SHORT_DRAMA_VOICE_CATALOG, digest
from arclight.persistence.models import Episode, WorkflowRun

CASTING_SCHEMA = "short-drama-voice-casting/1"
CASTING_PROMPT = (
    "为短剧缺少音色的角色和旁白选配音色。根据角色称呼、对白语言、语气和音色描述选择。"
    "每个角色标识恰好提供一项，只能用给定音色目录中的精确标识。可为多个角色选择同一音色。"
    "角色对白和目录描述仅供判断音色。只交付音色分配结果。"
)


class VoiceCastingContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class VoiceCastingRole(VoiceCastingContract):
    role_id: str = Field(min_length=1, max_length=128)
    speaker: str = Field(min_length=1, max_length=100)
    character_asset_id: UUID | None
    dialogue_samples: tuple[str, ...]


class VoiceCastingInput(VoiceCastingContract):
    audio_snapshot_id: UUID
    speech_service: dict = Field(repr=False)
    roles: tuple[VoiceCastingRole, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_roles(self) -> "VoiceCastingInput":
        if len({role.role_id for role in self.roles}) != len(self.roles):
            raise ValueError("待分配的角色标识不能重复")
        return self


class VoiceCastingChoice(VoiceCastingContract):
    role_id: str = Field(min_length=1, max_length=128)
    voice_id: str = Field(min_length=1, max_length=160)


class VoiceCastingResult(VoiceCastingContract):
    choices: tuple[VoiceCastingChoice, ...] = Field(min_length=1)


def _complete(_request: AgentModelRequest, result: BaseModel) -> AgentDecision[VoiceCastingResult]:
    assert isinstance(result, VoiceCastingResult)
    return AgentDecision[VoiceCastingResult](decision="COMPLETE", result=result, human_request=None)


CASTING_OUTPUT: AgentOutputProjection[AgentDecision[VoiceCastingResult]] = AgentOutputProjection(
    schema_version=CASTING_SCHEMA, wire_model=VoiceCastingResult, materialize=_complete
)


def _assignment_scope(
    db: Session, run_id: UUID, actor: str, command_id: UUID
) -> tuple[WorkflowRun, Episode, VoiceCastingInput]:
    from arclight.application.short_drama import require_exact_inputs
    from arclight.application.short_drama_audio import read_voice_casting_input
    from arclight.application.short_drama_delivery import require_run_authorization

    lock_gate(db)
    run, _ = require_producer(db, run_id, actor)
    episode, _, grant = require_run_authorization(db, run)
    require_exact_inputs(db, episode, grant)
    row = find_binding(db, episode.project_id, COMMAND, command_id)
    invalid = DomainError("SHORT_DRAMA_VOICE_SELECTION_INVALID", "缺少本次音色分配的原始命令", 409)
    if row is None:
        raise invalid
    intent = row.snapshot_json
    if (
        intent["revision"] != run.revision
        or intent["run_id"] != str(run.id)
        or intent["actor_id"] != actor
        or intent["payload_sha256"] != digest(intent["payload"])
    ):
        raise invalid
    try:
        command = ProducerVoiceCastingCommand.model_validate(intent["payload"])
    except ValidationError as error:
        raise invalid from error
    casting = read_voice_casting_input(db, episode.project_id, run.id)
    if command.casting_sha256 != digest(casting.model_dump(mode="json")):
        raise invalid
    return run, episode, casting


def _catalog_scope(run: WorkflowRun, episode: Episode, casting: VoiceCastingInput) -> dict:
    return {
        "run_id": str(run.id),
        "episode_id": str(episode.id),
        "casting_sha256": digest(casting.model_dump(mode="json")),
    }


def _read_catalog(
    db: Session, project_id: UUID, command_id: UUID, scope: dict
) -> AudioVoiceCatalogRead | None:
    binding = find_binding(db, project_id, SHORT_DRAMA_VOICE_CATALOG, command_id)
    if binding is None:
        return None
    saved = binding.snapshot_json
    if saved.get("scope") != scope:
        raise DomainError("SHORT_DRAMA_VOICE_SELECTION_INVALID", "冻结音色目录的命令不一致", 409)
    return AudioVoiceCatalogRead.model_validate(saved["catalog"])


def _catalog_snapshot(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, actor: str, command_id: UUID
) -> tuple[VoiceCastingInput, AudioVoiceCatalogRead, str]:
    with factory.begin() as db:
        run, episode, casting = _assignment_scope(db, run_id, actor, command_id)
        scope = _catalog_scope(run, episode, casting)
        catalog = _read_catalog(db, episode.project_id, command_id, scope)
        if catalog is not None:
            return casting, catalog, run.workflow_version
        service = restore_selection(casting.speech_service)
        source = read_voice_catalog_source(
            db, settings, episode.project_id, actor, selection=service
        )

    from arclight.application.short_drama_audio import require_casting_options

    # A supplier directory lookup must not hold scheduling, episode or project locks.
    queried = query_voice_catalog(source)
    with factory.begin() as db:
        run, episode, casting = _assignment_scope(db, run_id, actor, command_id)
        scope = _catalog_scope(run, episode, casting)
        catalog = _read_catalog(db, episode.project_id, command_id, scope)
        if catalog is None:
            require_casting_options(queried, service)
            bind(
                db,
                episode.project_id,
                SHORT_DRAMA_VOICE_CATALOG,
                command_id,
                {"scope": scope, "catalog": queried.model_dump(mode="json")},
            )
            catalog = queried
        return casting, catalog, run.workflow_version


def _selection(
    casting: VoiceCastingInput, catalog: AudioVoiceCatalogRead, result: VoiceCastingResult
) -> dict[str, str]:
    choices = {item.role_id: item.voice_id for item in result.choices}
    if (
        len(choices) != len(result.choices)
        or choices.keys() != {role.role_id for role in casting.roles}
        or not set(choices.values()) <= {option.id for option in catalog.options}
    ):
        raise DomainError(
            "SHORT_DRAMA_VOICE_SELECTION_INVALID", "文字模型返回的角色或音色不在本次候选范围", 422
        )
    return choices


def execute_voice_assignment(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, actor: str, command_id: UUID
) -> AgentLoopResult[VoiceCastingResult]:
    from arclight.application.short_drama_audio import publish_voice_casting

    casting, catalog, agent_version = _catalog_snapshot(
        factory, settings, run_id, actor, command_id
    )
    try:
        request = AgentModelRequest(
            run_id=run_id,
            agent_name="producer",
            stage="producer",
            operation_key=f"voice-casting:{command_id}",
            agent_version=agent_version,
            prompt_version=CASTING_SCHEMA,
            result_schema_version=CASTING_SCHEMA,
            system_instruction=CASTING_PROMPT,
            messages=[
                AgentUserMessage(
                    data={
                        "roles": [
                            role.model_dump(mode="json", exclude={"character_asset_id"})
                            for role in casting.roles
                        ],
                        "voices": [option.model_dump(mode="json") for option in catalog.options],
                    }
                )
            ],
        )
    except ValidationError as error:
        raise DomainError("AGENT_CONTEXT_LIMIT", "音色分配输入无法安全发送", 422) from error

    def validate(_db: Session, result: VoiceCastingResult) -> None:
        _selection(casting, catalog, result)

    outcome = run_persistent_agent_loop(
        factory,
        settings,
        request,
        AgentToolRegistry(),
        VoiceCastingResult,
        output_projection=CASTING_OUTPUT,
        include_project_reads=False,
        validate_result=validate,
    )
    if outcome.result is not None:
        with factory.begin() as db:
            run, _, exact = _assignment_scope(db, run_id, actor, command_id)
            publish_voice_casting(
                db, settings, run, _selection(exact, catalog, outcome.result), catalog=catalog
            )
    return outcome
