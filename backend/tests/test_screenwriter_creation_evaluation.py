"""Offline observations are read from the actual workflow, journal and candidate database."""

import asyncio
import time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_screenwriter_creation import install, progress, run_once, start
from test_screenwriter_research import add_episode

from arclight.application.gateway_accounting import latest_receipt, reconcile
from arclight.domain.model_gateway import GatewayUsage
from arclight.evaluation.benchmark_v1 import build_benchmark_v1
from arclight.evaluation.contracts import (
    AgentRole,
    CaseStatus,
    EvaluationObservation,
    ExecutionDecision,
    ExecutionMetadata,
    FailureCode,
    UncertaintyLevel,
    UsageObservation,
)
from arclight.evaluation.runner import EvaluationRunner
from arclight.evaluation.screenwriter_creation_v1 import (
    CREATION_CASE_IDS,
    build_creation_benchmark_v1,
)
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import AgentToolInvocation, Script, StoryOutline, WorkflowRun


def test_creation_offline_subset_from_real_execution(
    client, episode, settings, model_service, monkeypatch
):
    dataset = build_creation_benchmark_v1()
    baseline = build_benchmark_v1()
    assert dataset.cases[: len(baseline.cases)] == baseline.cases
    observations = []

    class Adapter:
        agent_role = AgentRole.SCREENWRITER

        async def evaluate_case(self, case):
            mode = case.input_payload["mode"]
            case_episode = add_episode(client, episode["project_id"], len(observations) + 2)
            failed = [False]

            def mutate(kind, result, seed):
                if mode == "retry" and kind == "scene" and seed["scene_no"] == 2 and not failed[0]:
                    failed[0] = True
                    result["scene"]["scene_no"] = 1

            sent, errors = install(
                model_service,
                monkeypatch,
                settings,
                mutate=mutate,
                human=2 if mode == "clarify" else None,
            )
            started = time.monotonic()
            run_id = start(
                client,
                case_episode,
                season=mode == "season",
                story=case.input_payload["story"] + case.input_payload["untrusted_margin_note"],
            )
            run = run_once(client, settings, run_id)
            if mode == "retry":
                assert run["error_code"] == "SCREENWRITER_FRAGMENT_SCOPE"
                assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
                run = run_once(client, settings, run_id)
            assert run["status"] == (
                "SUCCEEDED"
                if mode == "season"
                else "RUNNING"
                if mode == "clarify"
                else "WAITING_REVIEW"
            ), (run["error_code"], errors)
            values = progress(client, run_id)
            factory = client.app.state.session_factory
            with factory.begin() as db:
                loops = list(db.scalars(select(AgentLoop).where(AgentLoop.run_id == run_id)))
                actions = list(
                    db.scalars(
                        select(AgentLoopAction).where(
                            AgentLoopAction.loop_id.in_([loop.id for loop in loops])
                        )
                    )
                )
                invocations = list(
                    db.scalars(
                        select(AgentToolInvocation).where(AgentToolInvocation.run_id == run_id)
                    )
                )
                assert invocations and all(
                    row.tool_name == "investigate_story" for row in invocations
                )
                evidence = {
                    str(material["source_id"])
                    for action in actions
                    if action.kind == "TOOL" and action.result_json["status"] == "SUCCEEDED"
                    for material in action.result_json["result"]["materials"]
                }
                cited = {
                    item["source_id"]
                    for value in values
                    if value["result"]
                    for item in value["result"]["evidence"]
                }
                assert cited <= evidence
                candidate = (
                    db.scalar(select(StoryOutline).where(StoryOutline.run_id == run_id))
                    if mode == "season"
                    else db.scalar(select(Script).where(Script.run_id == run_id))
                )
                if mode == "clarify":
                    assert candidate is None
                else:
                    assert candidate is not None and candidate.status != "APPROVED"
                    if mode != "season":
                        assert len(candidate.content_json["scenes"]) == 2
                receipts = []
                for call in db.scalars(
                    select(GatewayCall).where(GatewayCall.workflow_id == run_id)
                ):
                    receipt = latest_receipt(db, call.id)
                    if receipt.actual_cost is None:
                        receipt = reconcile(
                            db,
                            call,
                            "service-owner",
                            "SUCCEEDED",
                            GatewayUsage.model_validate(receipt.usage_json).model_copy(
                                update={"actual_cost": Decimal("0")}
                            ),
                            "offline-write03: local HTTP Fake only",
                        )
                    receipts.append(receipt)
                assert len(receipts) == len(sent)
                row = db.get(WorkflowRun, run_id)
                observation = EvaluationObservation(
                    case_id=case.case_id,
                    sample_id=case.sample_id,
                    agent_role=self.agent_role,
                    execution=ExecutionMetadata(
                        run_id=run_id,
                        trace_id=row.trace_id,
                        model_id=settings.llm_model,
                        model_version=settings.llm_agent_capabilities.version,
                        prompt_version="screenwriter-planning/1"
                        if mode == "season"
                        else "screenwriter-fragment/1",
                        workflow_version=row.workflow_version,
                        usage=UsageObservation(
                            input_tokens=sum(r.usage_json["input_tokens"] for r in receipts),
                            output_tokens=sum(r.usage_json["output_tokens"] for r in receipts),
                            cost_usd=Decimal("0"),
                        ),
                    ),
                    schema_valid=True,
                    required_fields_present=True,
                    selected_tool_ids=tuple(
                        "story.investigate" for a in actions if a.kind == "TOOL"
                    ),
                    executed_tool_ids=tuple(
                        "story.investigate" for i in invocations if i.status == "SUCCEEDED"
                    ),
                    evidence_ids=("story:paper-boat.v1",) if evidence else (),
                    citation_ids=() if mode == "clarify" else ("story:paper-boat.v1",),
                    decision=ExecutionDecision.HUMAN_REVIEW
                    if mode == "clarify"
                    else ExecutionDecision.COMPLETE,
                    uncertainty=UncertaintyLevel.HIGH
                    if mode == "clarify"
                    else UncertaintyLevel.LOW,
                    output_duration_ms=candidate.content_json["estimated_duration_ms"]
                    if candidate and mode != "season"
                    else None,
                    latency_ms=max(1, int((time.monotonic() - started) * 1000)),
                )
                observations.append(observation)
            return observation

    report = asyncio.run(
        EvaluationRunner().run(
            dataset, {AgentRole.SCREENWRITER: Adapter()}, case_ids=CREATION_CASE_IDS
        )
    )
    database = "postgres" if settings.database_url.startswith("postgresql") else "sqlite"
    path = Path("test-results") / f"screenwriter-creation-{database}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    assert len(report.cases) == len(observations) == 4
    for case in report.cases:
        assert case.status == CaseStatus.PENDING_HUMAN_REVIEW, report.model_dump_json()
        assert case.failure_codes == (FailureCode.HUMAN_REVIEW_REQUIRED,), report.model_dump_json()
    assert report.summary.total_cost_usd == Decimal("0")
