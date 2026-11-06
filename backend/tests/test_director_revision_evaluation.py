"""Automatic observations derive from real check receipts, candidates and gateway accounting."""

import asyncio
import time
import traceback
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_director_creation import approved_script, progress, start
from test_director_revision import answer_body, checks, install_revision
from test_screenwriter_creation import run_once
from test_screenwriter_research import add_episode

from arclight.application.gateway_accounting import latest_receipt, reconcile
from arclight.domain.model_gateway import GatewayUsage
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
from arclight.evaluation.director_creation_v1 import build_director_creation_benchmark_v1
from arclight.evaluation.director_planning_v1 import SOURCE_ALIASES
from arclight.evaluation.director_revision_v1 import (
    REVISION_CASE_IDS,
    build_director_revision_benchmark_v1,
)
from arclight.evaluation.runner import EvaluationRunner
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import Storyboard, WorkflowRun


def test_director_iteration_offline_subset_uses_real_feedback_and_frozen_repair_counts(
    client, episode, settings, model_service, monkeypatch
):
    dataset = build_director_revision_benchmark_v1()
    baseline = build_director_creation_benchmark_v1()
    assert dataset.cases[: len(baseline.cases)] == baseline.cases
    observations, failures = [], []

    class Adapter:
        agent_role = AgentRole.DIRECTOR

        async def evaluate_case(self, case):
            try:
                return self.execute_case(case)
            except Exception:
                failures.append(traceback.format_exc())
                raise

        def execute_case(self, case):
            current = add_episode(client, episode["project_id"], len(observations) + 2)
            mode = case.input_payload["mode"]

            def configure(content):
                content["scenes"][0]["notes"] = case.input_payload["untrusted_note"]

            def mutate(result, seed):
                if mode == "recovery" or (
                    seed["scene"]["scene_no"] != 1 and mode not in {"multiround", "exhaustion"}
                ):
                    return
                shot = result["shots"][0]
                if mode == "duplicate":
                    shot["dialogue_line_ids"] *= 2
                elif mode == "duration":
                    shot["duration_ms"] = 0
                else:
                    shot["dialogue_line_ids"] = []

            sent, errors, base_errors = install_revision(
                model_service, monkeypatch, settings, mutate=mutate, escalate=mode == "recovery"
            )
            source = approved_script(client, current, configure=configure)
            run_id, _ = start(
                client, current, source, director_revision_rounds=1 if mode == "exhaustion" else 3
            )
            stamp = time.monotonic()
            run = run_once(client, settings, run_id)
            if mode == "recovery":
                question = checks(client, run_id)[-1]
                assert question["status"] == "WAITING_HUMAN"
                assert (
                    client.post(
                        f"/api/v1/runs/{run_id}/director/{question['loop_id']}/answers",
                        json=answer_body(question),
                    ).status_code
                    == 202
                )
                run = run_once(client, settings, run_id)
                assert checks(client, run_id)[-1]["loop_id"] == question["loop_id"]
            human = mode == "exhaustion"
            assert run["status"] == ("RUNNING" if human else "WAITING_REVIEW"), (
                run,
                errors,
                base_errors,
            )
            operations = progress(client, run_id)["operations"]
            reports = checks(client, run_id)
            citations = tuple(
                SOURCE_ALIASES[item["source_kind"]] for item in operations[0]["result"]["evidence"]
            )
            with client.app.state.session_factory.begin() as db:
                row = db.get(WorkflowRun, run_id)
                board = db.scalar(select(Storyboard).where(Storyboard.run_id == run_id))
                if human:
                    assert (
                        board is None and reports[-1]["director_revision"]["remaining_patches"] == 0
                    )
                else:
                    assert board is not None and board.status == "READY_FOR_REVIEW"
                    assert reports[-1]["result"]["decision"] == "ACCEPT"
                loops = list(db.scalars(select(AgentLoop).where(AgentLoop.run_id == run_id)))
                tools = list(
                    db.scalars(
                        select(AgentLoopAction).where(
                            AgentLoopAction.loop_id.in_([loop.id for loop in loops]),
                            AgentLoopAction.kind == "TOOL",
                        )
                    )
                )
                aliases = {
                    "investigate_production": "production.investigate",
                    "check_director_storyboard": "director.check",
                }
                assert {tool.request_json["tool_name"] for tool in tools} == set(aliases)
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
                            "offline-direct03: local HTTP MockTransport; no external model calls",
                        )
                    receipts.append(receipt)
                assert len(receipts) == len(sent) + 2
                observation = EvaluationObservation(
                    case_id=case.case_id,
                    sample_id=case.sample_id,
                    agent_role=self.agent_role,
                    execution=ExecutionMetadata(
                        run_id=run_id,
                        trace_id=row.trace_id,
                        model_id=settings.llm_model,
                        model_version=settings.llm_agent_capabilities.version,
                        prompt_version="director-revision/1",
                        workflow_version=row.workflow_version,
                        usage=UsageObservation(
                            input_tokens=sum(r.usage_json["input_tokens"] for r in receipts),
                            output_tokens=sum(r.usage_json["output_tokens"] for r in receipts),
                            cost_usd=sum((r.actual_cost for r in receipts), Decimal("0")),
                        ),
                    ),
                    schema_valid=True,
                    required_fields_present=True,
                    selected_tool_ids=tuple(
                        aliases[tool.request_json["tool_name"]] for tool in tools
                    ),
                    executed_tool_ids=tuple(
                        aliases[tool.request_json["tool_name"]]
                        for tool in tools
                        if tool.result_json["status"] == "SUCCEEDED"
                    ),
                    citation_ids=citations,
                    evidence_ids=citations,
                    decision=ExecutionDecision.HUMAN_REVIEW
                    if human
                    else ExecutionDecision.COMPLETE,
                    uncertainty=UncertaintyLevel.HIGH if human else UncertaintyLevel.LOW,
                    output_duration_ms=sum(
                        shot["duration_ms"] for shot in board.content_json["shots"]
                    )
                    if board
                    else None,
                    latency_ms=max(1, int((time.monotonic() - stamp) * 1000)),
                )
                observations.append(observation)
                return observation

    report = asyncio.run(
        EvaluationRunner().run(dataset, {AgentRole.DIRECTOR: Adapter()}, case_ids=REVISION_CASE_IDS)
    )
    database = "postgres" if settings.database_url.startswith("postgresql") else "sqlite"
    path = Path("test-results") / f"director-revision-{database}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    assert not failures, "\n\n".join(failures)
    assert len(report.cases) == len(observations) == 6
    for case in report.cases:
        assert case.status == CaseStatus.PENDING_HUMAN_REVIEW, report.model_dump_json()
        assert case.failure_codes == (FailureCode.HUMAN_REVIEW_REQUIRED,), report.model_dump_json()
        assert all(score.passed is not False for score in case.scores)
    assert report.summary.total_cost_usd == Decimal("0")
