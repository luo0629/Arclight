"""Offline planning observations come from actual persistent execution, not authored scores."""

import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import select
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_agent_tools import create_entry
from test_screenwriter_planning import plan_value, prepare
from test_screenwriter_research import add_episode

from arclight.application.gateway_accounting import latest_receipt, reconcile
from arclight.application.screenwriter_planning import plan_screenwriter, read_screenwriter_plan
from arclight.domain.agent_model import AgentModelRequest
from arclight.domain.model_gateway import GatewayUsage
from arclight.domain.screenwriter_research import InvestigateStoryResult
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
from arclight.evaluation.screenwriter_planning_v1 import (
    PLANNING_CASE_IDS,
    build_planning_benchmark_v1,
)
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import AgentToolInvocation, WorkflowRun


def test_offline_planning_subset_runs_actual_modes_tools_saved_plans_and_human_questions(
    client, episode, settings, model_service
):
    dataset = build_planning_benchmark_v1()
    baseline = build_benchmark_v1()
    assert dataset.cases[: len(baseline.cases)] == baseline.cases
    observations = []

    class Adapter:
        agent_role = AgentRole.SCREENWRITER

        async def evaluate_case(self, case):
            case_episode = add_episode(client, episode["project_id"], len(observations) + 2)
            mode = case.input_payload["mode"]
            missing = mode == "clarify"
            entry_id = create_entry(
                client,
                episode["project_id"],
                data={
                    "description": case.input_payload["story"],
                    "rules": [case.input_payload["untrusted_margin_note"]],
                },
            )
            run_id, actor = prepare(
                client,
                case_episode,
                settings,
                "episode" if missing else mode,
                target="dialogue" if mode == "rewrite" else None,
            )

            def respond(request):
                payload = json.loads(request.content)
                feedback = tool_feedback(payload)
                if not feedback:
                    value = call_response(
                        (
                            "research-plan",
                            "investigate_story",
                            {
                                "query": {"path": "entries", "name": "不存在"}
                                if missing
                                else {
                                    "path": "exact",
                                    "source_kind": "STORY_ENTRY",
                                    "source_id": str(entry_id),
                                }
                            },
                        )
                    )
                elif missing:
                    assert feedback[-1]["result"]["missing"]
                    value = final("HUMAN_REVIEW")
                else:
                    value = final(result=plan_value(payload))
                return httpx.Response(200, json=value)

            model_service(respond)
            factory = client.app.state.session_factory
            started = time.monotonic()
            outcome = plan_screenwriter(factory, settings, run_id, actor)
            assert outcome.status == ("HUMAN_REVIEW" if missing else "COMPLETED")
            with factory.begin() as db:
                assert read_screenwriter_plan(db, outcome.loop_id, actor) == outcome
                run = db.get(WorkflowRun, run_id)
                loop = db.get(AgentLoop, outcome.loop_id)
                seed = AgentModelRequest.model_validate(loop.seed_json)
                actions = list(
                    db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id))
                )
                selected = [row.request_json["tool_name"] for row in actions if row.kind == "TOOL"]
                ids = {
                    str(material.source_id)
                    for row in actions
                    if row.kind == "TOOL" and row.result_json["status"] == "SUCCEEDED"
                    for material in InvestigateStoryResult.model_validate(
                        row.result_json["result"]
                    ).materials
                }
                cited = (
                    {str(item.source_id) for item in outcome.result.evidence}
                    if outcome.result
                    else set()
                )
                assert cited <= ids and (not ids) == missing
                invocations = list(
                    db.scalars(
                        select(AgentToolInvocation).where(AgentToolInvocation.run_id == run_id)
                    )
                )
                receipts = []
                for call in db.scalars(
                    select(GatewayCall).where(GatewayCall.workflow_id == run_id)
                ):
                    receipt = latest_receipt(db, call.id)
                    assert receipt is not None and receipt.actual_cost is None
                    receipts.append(
                        reconcile(
                            db,
                            call,
                            actor,
                            "SUCCEEDED",
                            GatewayUsage.model_validate(receipt.usage_json).model_copy(
                                update={"actual_cost": Decimal("0")}
                            ),
                            "offline-write02: local HTTP MockTransport, no external model calls",
                        )
                    )
                assert len(receipts) == outcome.model_turns == 2
                aliases = {
                    str(entry_id): "story:paper-boat.v1",
                    "investigate_story": "story.investigate",
                }
                observation = EvaluationObservation(
                    case_id=case.case_id,
                    sample_id=case.sample_id,
                    agent_role=self.agent_role,
                    execution=ExecutionMetadata(
                        run_id=run_id,
                        trace_id=run.trace_id,
                        model_id=settings.llm_model,
                        model_version=settings.llm_agent_capabilities.version,
                        prompt_version=seed.prompt_version,
                        workflow_version=run.workflow_version,
                        usage=UsageObservation(
                            input_tokens=sum(row.usage_json["input_tokens"] for row in receipts),
                            output_tokens=sum(row.usage_json["output_tokens"] for row in receipts),
                            cost_usd=sum((row.actual_cost for row in receipts), Decimal("0")),
                        ),
                    ),
                    schema_valid=True,
                    required_fields_present=True,
                    selected_tool_ids=tuple(aliases.get(name, name) for name in selected),
                    executed_tool_ids=tuple(
                        aliases.get(row.tool_name, row.tool_name)
                        for row in invocations
                        if row.status == "SUCCEEDED"
                    ),
                    citation_ids=tuple(
                        aliases.get(identity, identity) for identity in sorted(cited)
                    ),
                    evidence_ids=tuple(aliases.get(identity, identity) for identity in sorted(ids)),
                    decision=ExecutionDecision.HUMAN_REVIEW
                    if outcome.status == "HUMAN_REVIEW"
                    else ExecutionDecision.COMPLETE,
                    uncertainty=UncertaintyLevel.HIGH
                    if outcome.human_request
                    else UncertaintyLevel.LOW,
                    output_duration_ms=outcome.result.plan.estimated_duration_ms
                    if outcome.result and mode in {"episode", "rewrite"}
                    else None,
                    latency_ms=max(1, int((time.monotonic() - started) * 1000)),
                )
                observations.append(observation)
                # Terminate only this isolated fixture's run to allow the next case.
                run.status = "CANCELLED"
            return observation

    report = asyncio.run(
        EvaluationRunner().run(
            dataset, {AgentRole.SCREENWRITER: Adapter()}, case_ids=PLANNING_CASE_IDS
        )
    )
    database = "postgres" if settings.database_url.startswith("postgresql") else "sqlite"
    report_path = Path("test-results") / f"screenwriter-planning-{database}.json"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    assert len(report.cases) == len(observations) == 4, report.model_dump_json()
    for case in report.cases:
        assert case.status == CaseStatus.PENDING_HUMAN_REVIEW, report.model_dump_json()
        assert case.failure_codes == (FailureCode.HUMAN_REVIEW_REQUIRED,)
        assert all(score.passed is not False for score in case.scores)
    assert report.summary.total_cost_usd == Decimal("0")
