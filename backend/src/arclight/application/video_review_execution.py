"""Run bounded video QA in the existing durable continuity workflow."""

import logging
from hashlib import sha256
from uuid import UUID, uuid5

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_runtime import run_persistent_agent_loop
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import AgentToolRegistry
from arclight.application.agent_workflows import (
    WorkflowExecutionDeferred,
    WorkflowRecoveryFailed,
    active_workflow,
    fail_workflow,
)
from arclight.application.commands import emit
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.application.model_services import run_settings
from arclight.application.production_versions import checked_snapshot, find_binding, text_settings
from arclight.application.video_review_evidence import (
    TEMPORAL_IMAGES,
    VideoEvidence,
    approved_shot_context,
    collect_evidence,
)
from arclight.application.video_review_routing import record_temporal_review, temporal_report_id
from arclight.application.video_review_work import review_inputs
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage, ContextImage
from arclight.domain.agent_state import AgentStateError, restore_agent_state, transition_agent_step
from arclight.domain.versions import digest
from arclight.domain.video_review import VIDEO_ADMISSION_RESOURCE
from arclight.domain.video_temporal_reviewer import (
    TEMPORAL_AGENT,
    TEMPORAL_PROMPT,
    TEMPORAL_WORK_RESOURCE,
    TemporalAssessment,
    TemporalWork,
)
from arclight.persistence.models import Episode, VersionBinding, WorkflowRun
from arclight.providers.agent_model import agent_transport, check_capabilities

logger = logging.getLogger(__name__)


def assess_video(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    work: TemporalWork,
    evidence: VideoEvidence,
) -> tuple[TemporalAssessment | None, str | None]:
    if len(evidence.samples) < 3 or not evidence.end_covered:
        return None, "有效样本缺少起点、中段或末段，请人工查看完整视频。"
    if (
        evidence.shot_context["dynamic_required"]
        and len({frame.frame_sha256 for frame in evidence.samples}) == 1
    ):
        return None, "必需动态镜头的抽样帧内容相同，须人工核对动作和静止问题。"
    if not evidence.references:
        return None, "无法取得冻结的首帧参考，请人工复核。"
    image = ContextImage(
        uuid5(work.target.candidate_id, evidence.sheet_sha256), "image/jpeg", evidence.sheet
    )
    images = {item.file_id: item for item in (image, *evidence.references)}
    seed: dict[str, JsonValue] = {
        "target": work.target.model_dump(mode="json"),
        "shot_context": evidence.shot_context,
        "samples": [frame.model_dump(mode="json") for frame in evidence.samples],
        "sheet_sha256": evidence.sheet_sha256,
        "reference_sha256": [sha256(item.data).hexdigest() for item in evidence.references],
    }
    request = AgentModelRequest(
        run_id=run_id,
        agent_name="continuity_reviewer",
        stage="storyboard",
        operation_key=f"{TEMPORAL_AGENT}:{work.target.candidate_id}",
        agent_version=TEMPORAL_AGENT,
        prompt_version=TEMPORAL_AGENT,
        result_schema_version="video-temporal-assessment/1",
        system_instruction=TEMPORAL_PROMPT,
        messages=[AgentUserMessage(data=seed, image_file_ids=list(images))],
    )
    if agent_transport(settings) is None:
        return None, "当前审查模型没有已配置的时序样本视觉协议，请人工观看。"
    try:
        check_capabilities(settings, request, ())
    except DomainError as error:
        if error.code != "GATEWAY_CAPABILITY_REJECTED":
            raise
        return None, "当前模型缺少所需的视觉输入能力，请人工观看。"

    def valid_assessment(db: Session, assessment: TemporalAssessment) -> None:
        if [item.time_ms for item in assessment.observations] != [
            item.time_ms for item in evidence.samples
        ]:
            raise DomainError(
                "VIDEO_REVIEW_EVIDENCE_INVALID", "审查结果未核对全部精确时间样本", 409
            )

    scope = TEMPORAL_IMAGES.set((run_id, digest(seed), images))
    try:
        result = run_persistent_agent_loop(
            factory,
            settings,
            request,
            AgentToolRegistry(),
            TemporalAssessment,
            validate_result=valid_assessment,
        )
    except DomainError as error:
        if error.code != "VIDEO_REVIEW_EVIDENCE_INVALID":
            raise
        return None, "模型未能引用全部精确样本，请人工观看。"
    finally:
        TEMPORAL_IMAGES.reset(scope)
    if result.stop_code == "ACCOUNT_BALANCE_INSUFFICIENT":
        raise DomainError("ACCOUNT_BALANCE_INSUFFICIENT", "账户余额不足", 402)
    if result.status != "COMPLETED" or result.result is None:
        return None, f"未取得完整模型结论（{result.stop_code or result.status}），请人工观看。"
    return result.result, None


def execute_video_review(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, revision: int
) -> None:
    try:
        with factory.begin() as db:
            lock_gate(db)
            run = db.get(WorkflowRun, run_id, populate_existing=True, with_for_update=True)
            if run is None or run.revision != revision or run.status not in {"QUEUED", "RUNNING"}:
                return
            validate_publication(db, run_id)
            episode = db.get(Episode, run.episode_id)
            assert episode is not None
            project_id = episode.project_id
            actor = execution_actor(db, run)
            settings = run_settings(
                text_settings(db, project_id, run, settings),
                run.input_json.get("_text_service"),
                db,
            )
            scope = scope_for_run(run, project_id)
            state = restore_agent_state(run.agent_state_json, None, scope)
            if state.steps[0].status == "PENDING":
                state = transition_agent_step(
                    state,
                    scope,
                    expected_revision=state.revision,
                    step_id="storyboard",
                    target="RUNNING",
                )
                run.agent_state_json = state.model_dump(mode="json")
            if run.status == "QUEUED":
                emit(db, run, "workflow.started", {"revision": revision})
            run.status, run.pending_review = "RUNNING", None
            items = [
                TemporalWork.model_validate(checked_snapshot(db, row)["temporal_work"])
                for row in db.scalars(
                    select(VersionBinding)
                    .where(
                        VersionBinding.project_id == project_id,
                        VersionBinding.resource_type == TEMPORAL_WORK_RESOURCE,
                        VersionBinding.snapshot_json["temporal_work"]["run_id"].as_string()
                        == str(run_id),
                    )
                    .order_by(VersionBinding.created_at, VersionBinding.id)
                )
                if find_binding(
                    db,
                    project_id,
                    VIDEO_ADMISSION_RESOURCE,
                    temporal_report_id(run, row.resource_id),
                )
                is None
            ]
        progress = {"resource_type": "continuity_review", "destination": "VIDEO_TEMPORAL_REVIEW"}
        for item in items:
            with factory() as db:
                inputs = review_inputs(db, project_id, item.target.candidate_id)
                if inputs.target != item.target:
                    raise DomainError(
                        "VIDEO_REVIEW_TARGET_INVALID", "待审视频与精确送审记录不匹配", 409
                    )
                evidence = collect_evidence(db, settings, inputs)
            assessment, gap = assess_video(factory, settings, run_id, item, evidence)
            with factory.begin() as db:
                run = active_workflow(db, run_id, revision)
                lock_dependency_graph(db, project_id)
                inputs = review_inputs(db, project_id, item.target.candidate_id)
                if (
                    inputs.target != item.target
                    or approved_shot_context(db, inputs.shot) != evidence.shot_context
                ):
                    raise DomainError(
                        "VIDEO_REVIEW_TARGET_INVALID", "取证后精确候选或镜头内容已变化", 409
                    )
                report_id = temporal_report_id(run, item.target.candidate_id)
                if find_binding(db, project_id, VIDEO_ADMISSION_RESOURCE, report_id) is not None:
                    continue
                record = record_temporal_review(
                    db,
                    run,
                    actor,
                    inputs,
                    evidence,
                    assessment,
                    gap,
                    {
                        "mode": "vision-advisory" if assessment is not None else "human-required",
                        "provider": settings.provider,
                        "supplier": settings.llm_supplier,
                        "model": settings.llm_model,
                        "configuration_sha256": digest(
                            {
                                "endpoint": settings.llm_base_url,
                                "format": settings.llm_response_format,
                                "capabilities": settings.llm_agent_capabilities.model_dump(
                                    mode="json"
                                )
                                if settings.llm_agent_capabilities
                                else None,
                            }
                        ),
                        "result_schema_sha256": digest(TemporalAssessment.model_json_schema()),
                    },
                )
                progress |= {"report_id": str(report_id), "conclusion": record.report.conclusion}
        with factory.begin() as db:
            run = active_workflow(db, run_id, revision)
            run.status, run.pending_review = "WAITING_REVIEW", progress
            emit(db, run, "workflow.waiting_review", progress)
    except DomainError as error:
        if error.code in {"AGENT_LOOP_BUSY", "AGENT_EXECUTION_LOST", "VIDEO_SAMPLER_BUSY"}:
            raise WorkflowExecutionDeferred(error.code) from None
        if error.code != "SCREENWRITER_DELIVERY_STALE":
            fail_workflow(factory, run_id, revision, error.code)
    except AgentStateError:
        fail_workflow(factory, run_id, revision, "RUN_STATE_INVALID")
    except WorkflowRecoveryFailed:
        raise
    except Exception as error:
        logger.error("video_review_failed run_id=%s error_type=%s", run_id, type(error).__name__)
        fail_workflow(factory, run_id, revision, "WORKFLOW_EXECUTION_FAILED")
