"""Offline automatic metrics use real candidates, questions, tools and accounting receipts."""

import asyncio
import time
import traceback
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_director_creation import approved_script, install, progress, start
from test_director_creation_recovery import answer_body, endpoint
from test_screenwriter_creation import run_once
from test_screenwriter_research import add_episode
from test_text_contract_v2 import make_asset

from arclight.agents.contracts import ScriptContent, StoryboardContent
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
from arclight.evaluation.director_creation_v1 import (
    CREATION_CASE_IDS,
    build_director_creation_benchmark_v1,
)
from arclight.evaluation.director_planning_v1 import SOURCE_ALIASES
from arclight.evaluation.runner import EvaluationRunner
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import Storyboard, WorkflowRun


def test_offline_director_creation_observes_complete_scenes_exact_assets_clarification_and_recovery(
    client, episode, settings, model_service, monkeypatch
):
    dataset = build_director_creation_benchmark_v1()
    baseline = build_benchmark_v1()
    assert dataset.cases[: len(baseline.cases)] == baseline.cases
    observations = []
    errors = []

    class Adapter:
        agent_role = AgentRole.DIRECTOR

        async def evaluate_case(self, case):
            try:
                return self.execute_case(case)
            except Exception:
                errors.append(traceback.format_exc())
                raise

        def execute_case(self, case):
            current = add_episode(client, episode["project_id"], len(observations) + 2)
            mode = case.input_payload["mode"]
            reference = None
            if mode == "assets":
                with client.app.state.session_factory.begin() as db:
                    reference = make_asset(db, current["project_id"], "SCENE")

            def configure(content):
                content["scenes"][0]["notes"] = case.input_payload["untrusted_note"]
                if reference:
                    for scene in content["scenes"]:
                        scene["scene_asset"] = reference

            source = approved_script(client, current, configure=configure)
            sent, errors = install(
                model_service,
                monkeypatch,
                settings,
                human=2 if mode in {"clarify", "recovery"} else None,
            )
            run_id, _ = start(client, current, source)
            stamp = time.monotonic()
            run = run_once(client, settings, run_id)
            if mode == "recovery":
                question = progress(client, run_id)["operations"][-1]
                assert (
                    client.post(endpoint(run_id, question), json=answer_body(question)).status_code
                    == 202
                )
                run = run_once(client, settings, run_id)
                assert progress(client, run_id)["operations"][2]["loop_id"] == question["loop_id"]
            assert run["status"] == ("RUNNING" if mode == "clarify" else "WAITING_REVIEW"), (
                run,
                errors,
            )
            operations = progress(client, run_id)["operations"]
            citations = tuple(
                SOURCE_ALIASES[item["source_kind"]] for item in operations[0]["result"]["evidence"]
            )
            with client.app.state.session_factory.begin() as db:
                row = db.get(WorkflowRun, run_id)
                candidate = db.scalar(select(Storyboard).where(Storyboard.run_id == run_id))
                content = (
                    StoryboardContent.model_validate(candidate.content_json).validate_against(
                        ScriptContent.model_validate(source["content_json"])
                    )
                    if candidate
                    else None
                )
                if content:
                    assert {shot.scene_id for shot in content.shots} == {
                        scene["scene_id"] for scene in source["content_json"]["scenes"]
                    }
                    assert candidate.status == "READY_FOR_REVIEW"
                    if reference:
                        assert all(
                            shot.scene_asset.model_dump(mode="json") == reference
                            for shot in content.shots
                        )
                else:
                    assert mode == "clarify" and operations[-1]["status"] == "WAITING_HUMAN"
                loops = list(db.scalars(select(AgentLoop).where(AgentLoop.run_id == run_id)))
                actions = list(
                    db.scalars(
                        select(AgentLoopAction).where(
                            AgentLoopAction.loop_id.in_([loop.id for loop in loops])
                        )
                    )
                )
                tools = [action for action in actions if action.kind == "TOOL"]
                assert {tool.request_json["tool_name"] for tool in tools} == {
                    "investigate_production"
                }
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
                            "offline-direct02: local HTTP MockTransport; no external model calls",
                        )
                    receipts.append(receipt)
                assert len(receipts) == len(sent)
                observation = EvaluationObservation(
                    case_id=case.case_id,
                    sample_id=case.sample_id,
                    agent_role=self.agent_role,
                    execution=ExecutionMetadata(
                        run_id=run_id,
                        trace_id=row.trace_id,
                        model_id=settings.llm_model,
                        model_version=settings.llm_agent_capabilities.version,
                        prompt_version="director-fragment/1",
                        workflow_version=row.workflow_version,
                        usage=UsageObservation(
                            input_tokens=sum(r.usage_json["input_tokens"] for r in receipts),
                            output_tokens=sum(r.usage_json["output_tokens"] for r in receipts),
                            cost_usd=sum((r.actual_cost for r in receipts), Decimal("0")),
                        ),
                    ),
                    schema_valid=True,
                    required_fields_present=True,
                    selected_tool_ids=tuple("production.investigate" for _ in tools),
                    executed_tool_ids=tuple(
                        "production.investigate"
                        for tool in tools
                        if tool.result_json["status"] == "SUCCEEDED"
                    ),
                    citation_ids=citations,
                    evidence_ids=citations,
                    asset_reference_ids=(SOURCE_ALIASES["ASSET"],) if reference else (),
                    decision=ExecutionDecision.HUMAN_REVIEW
                    if mode == "clarify"
                    else ExecutionDecision.COMPLETE,
                    uncertainty=UncertaintyLevel.HIGH
                    if mode == "clarify"
                    else UncertaintyLevel.LOW,
                    output_duration_ms=sum(shot.duration_ms for shot in content.shots)
                    if content
                    else None,
                    latency_ms=max(1, int((time.monotonic() - stamp) * 1000)),
                )
                observations.append(observation)
            return observation

    report = asyncio.run(
        EvaluationRunner().run(dataset, {AgentRole.DIRECTOR: Adapter()}, case_ids=CREATION_CASE_IDS)
    )
    database = "postgres" if settings.database_url.startswith("postgresql") else "sqlite"
    path = Path("test-results") / f"director-creation-{database}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    assert not errors, "\n\n".join(errors)
    assert len(report.cases) == len(observations) == 4, errors
    for case in report.cases:
        assert case.status == CaseStatus.PENDING_HUMAN_REVIEW, report.model_dump_json()
        assert case.failure_codes == (FailureCode.HUMAN_REVIEW_REQUIRED,), report.model_dump_json()
        assert all(score.passed is not False for score in case.scores)
    assert report.summary.total_cost_usd == Decimal("0")
