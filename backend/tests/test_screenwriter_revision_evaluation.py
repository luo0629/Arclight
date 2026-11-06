"""Use actual journals, tool receipts and candidates for the offline revision subset."""

import asyncio
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_screenwriter_creation import progress, run_once, start
from test_screenwriter_research import add_episode
from test_screenwriter_revision import install_revision
from test_script_rewrite import body as rewrite_body
from test_script_rewrite import source as rewrite_source
from test_script_rewrite import start as start_rewrite

from arclight.application.gateway_accounting import latest_receipt, reconcile
from arclight.domain.model_gateway import GatewayUsage
from arclight.domain.screenwriter_revision import PATCH_TOOL
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
from arclight.evaluation.screenwriter_revision_v1 import (
    REVISION_CASE_IDS,
    build_revision_benchmark_v1,
)
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import AgentToolInvocation, Script, WorkflowRun


def test_revision_offline_subset_observes_real_tools_patches_recovery_and_escalation(
    client, episode, settings, model_service, monkeypatch
):
    dataset = build_revision_benchmark_v1()
    baseline = build_benchmark_v1()
    assert dataset.cases[: len(baseline.cases)] == baseline.cases
    observations = []

    class Adapter:
        agent_role = AgentRole.SCREENWRITER

        async def evaluate_case(self, case):
            mode = case.input_payload["mode"]
            selected_episode = add_episode(client, episode["project_id"], len(observations) + 2)
            failed = []

            def mutate(decision, seed):
                if (
                    mode == "recovery"
                    and seed["round"] == 1
                    and decision.get("result")
                    and not failed
                ):
                    failed.append(True)
                    decision["result"]["source_sha256"] = "f" * 64

            sent, errors, ordinary_errors = install_revision(
                model_service,
                monkeypatch,
                settings,
                patches=2 if mode == "exhausted" else 1,
                mutate_report=mutate,
            )
            started = time.monotonic()
            if mode == "rewrite":
                base = rewrite_source(client, selected_episode)
                response = start_rewrite(
                    client, selected_episode, rewrite_body(base) | {"professional": True}
                )
                assert response.status_code == 202
                run_id = UUID(response.json()["run_id"])
            else:
                run_id = start(
                    client,
                    selected_episode,
                    revision_rounds=1,
                    story=case.input_payload["story"] + case.input_payload["untrusted_margin_note"],
                )
            run = run_once(client, settings, run_id)
            if mode == "recovery":
                assert run["error_code"] == "SCREENWRITER_CRITIQUE_SCOPE"
                assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
                run = run_once(client, settings, run_id)
            assert run["status"] == ("RUNNING" if mode == "exhausted" else "WAITING_REVIEW"), (
                run,
                errors,
                ordinary_errors,
            )
            values = progress(client, run_id)
            with client.app.state.session_factory.begin() as db:
                loops = list(db.scalars(select(AgentLoop).where(AgentLoop.run_id == run_id)))
                actions = list(
                    db.scalars(
                        select(AgentLoopAction).where(
                            AgentLoopAction.loop_id.in_([row.id for row in loops])
                        )
                    )
                )
                invocations = list(
                    db.scalars(
                        select(AgentToolInvocation).where(AgentToolInvocation.run_id == run_id)
                    )
                )
                evidence = {
                    str(material["source_id"])
                    for action in actions
                    if action.kind == "TOOL"
                    and action.request_json["tool_name"] == "investigate_story"
                    and action.result_json["status"] == "SUCCEEDED"
                    for material in action.result_json["result"]["materials"]
                }
                cited = {
                    item["source_id"]
                    for value in values
                    if value["result"]
                    for item in value["result"].get("evidence", [])
                }
                assert cited and cited <= evidence
                assert any(
                    row.tool_name == PATCH_TOOL and row.status == "SUCCEEDED" for row in invocations
                )
                assert all(
                    row.tool_name in {"investigate_story", PATCH_TOOL} for row in invocations
                )
                candidate = db.scalar(select(Script).where(Script.run_id == run_id))
                assert (candidate is None) == (mode == "exhausted")
                if candidate:
                    assert candidate.status == "READY_FOR_REVIEW"
                reports = [value for value in values if value["kind"] == "critique"]
                assert reports[0]["result"]["decision"] == "REVISE"
                assert reports[-1]["status"] == (
                    "WAITING_HUMAN" if mode == "exhausted" else "COMPLETED"
                )
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
                            "offline-write04: local HTTP Fake only",
                        )
                    receipts.append(receipt)
                assert len(receipts) == len(sent)
                row = db.get(WorkflowRun, run_id)
                tool_ids = {
                    "investigate_story": "story.investigate",
                    PATCH_TOOL: "script.check_patch",
                }
                observation = EvaluationObservation(
                    case_id=case.case_id,
                    sample_id=case.sample_id,
                    agent_role=self.agent_role,
                    execution=ExecutionMetadata(
                        run_id=run_id,
                        trace_id=row.trace_id,
                        model_id=settings.llm_model,
                        model_version=settings.llm_agent_capabilities.version,
                        prompt_version="screenwriter-revision/1",
                        workflow_version=row.workflow_version,
                        usage=UsageObservation(
                            input_tokens=sum(item.usage_json["input_tokens"] for item in receipts),
                            output_tokens=sum(
                                item.usage_json["output_tokens"] for item in receipts
                            ),
                            cost_usd=Decimal("0"),
                        ),
                    ),
                    schema_valid=True,
                    required_fields_present=True,
                    selected_tool_ids=tuple(
                        tool_ids[action.request_json["tool_name"]]
                        for action in actions
                        if action.kind == "TOOL"
                    ),
                    executed_tool_ids=tuple(
                        tool_ids[item.tool_name]
                        for item in invocations
                        if item.status == "SUCCEEDED"
                    ),
                    evidence_ids=("input:paper-boat.v1",),
                    citation_ids=("input:paper-boat.v1",),
                    decision=ExecutionDecision.HUMAN_REVIEW
                    if mode == "exhausted"
                    else ExecutionDecision.COMPLETE,
                    uncertainty=UncertaintyLevel.HIGH
                    if mode == "exhausted"
                    else UncertaintyLevel.LOW,
                    output_duration_ms=(
                        candidate.content_json
                        if candidate
                        else reports[-1]["revision"]["working_script"]
                    )["estimated_duration_ms"],
                    latency_ms=max(1, int((time.monotonic() - started) * 1000)),
                )
                observations.append(observation)
                return observation

    report = asyncio.run(
        EvaluationRunner().run(
            dataset, {AgentRole.SCREENWRITER: Adapter()}, case_ids=REVISION_CASE_IDS
        )
    )
    database = "postgres" if settings.database_url.startswith("postgresql") else "sqlite"
    path = Path("test-results") / f"screenwriter-revision-{database}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    assert len(report.cases) == len(observations) == 4
    for case in report.cases:
        assert case.status == CaseStatus.PENDING_HUMAN_REVIEW, report.model_dump_json()
        assert case.failure_codes == (FailureCode.HUMAN_REVIEW_REQUIRED,), report.model_dump_json()
    assert report.summary.total_cost_usd == Decimal("0")
