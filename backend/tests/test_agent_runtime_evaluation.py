"""Run an AEVAL-01 subset through ARUN-03; only model transport and domain tools are fixtures."""

import asyncio
import json
import time
from dataclasses import replace
from decimal import Decimal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from test_agent_model import call_response, model_request
from test_agent_model import model_service as model_service
from test_agent_runtime import rows_for, setup_loop, tool_feedback
from test_agent_tools import create_entry, grant

from arclight.application.agent_runtime import run_agent_loop
from arclight.application.agent_tools import AgentToolDefinition
from arclight.application.gateway_accounting import latest_receipt, reconcile
from arclight.core.errors import DomainError
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.model_gateway import GatewayUsage
from arclight.evaluation.benchmark_v1 import build_benchmark_v1
from arclight.evaluation.contracts import (
    AgentRole,
    CaseStatus,
    EvaluationObservation,
    ExecutionDecision,
    ExecutionMetadata,
    FailureCode,
    UsageObservation,
)
from arclight.evaluation.runner import EvaluationRunner
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import AgentToolInvocation, StoryEntry, WorkflowRun


class CitationParameters(AgentToolContract):
    citation_ids: list[str] = Field(min_length=1, max_length=8)
    duration_ms: int = Field(strict=True, ge=1)


class CitationResult(AgentToolContract):
    citation_ids: list[str]
    duration_ms: int = Field(strict=True)
    valid: bool


class EvaluatedDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=300)
    citation_ids: list[str] = Field(min_length=1, max_length=8)
    duration_ms: int = Field(strict=True, ge=1, le=3_600_000)


def test_actual_runtime_executes_screenwriter_benchmark_subset(
    client,
    episode,
    settings,
    model_service,
    tmp_path,
):
    dataset = build_benchmark_v1()
    case = next(case for case in dataset.cases if case.agent_role == AgentRole.SCREENWRITER)
    run_id, actor, registry = setup_loop(client, episode, settings)
    # Test tools provide bounded synthetic domain fixtures, not a completed professional Agent.
    entry_id = create_entry(
        client,
        episode["project_id"],
        data={
            "story": case.input_payload["story"],
            "source_ref": "story:paper-boat.v1",
            "target_duration_ms": case.input_payload["target_duration_ms"],
        },
    )
    registry.register(replace(registry.get("read_story_entry"), name="story_read_version"))

    def check(context, parameters):
        assert isinstance(parameters, CitationParameters)
        source = context.db.get(StoryEntry, entry_id)
        if (
            source.project_id != context.project_id
            or parameters.citation_ids != [source.data_json["source_ref"]]
            or parameters.duration_ms != source.data_json["target_duration_ms"]
        ):
            raise DomainError("CITATION_INVALID", "引用或时长不符", 422)
        return {**parameters.model_dump(), "valid": True}

    registry.register(
        AgentToolDefinition(
            name="citation_validate",
            allowed_agents=frozenset({"screenwriter"}),
            allowed_steps=frozenset({"script"}),
            parameters_model=CitationParameters,
            result_model=CitationResult,
            scope_validator=lambda context, parameters: None,
            handler=check,
        )
    )
    grant(client, run_id, actor, registry, allowed=("story_read_version", "citation_validate"))
    selected = []

    def respond(request):
        payload = json.loads(request.content)
        history = tool_feedback(payload)
        if not history:
            name = "story_read_version"
            value = call_response(("read_story", name, {"entry_id": str(entry_id)}))
            selected.append(name)
        elif history[-1]["tool_name"] == "story_read_version":
            source = history[-1]["result"]["data"]
            name = "citation_validate"
            value = call_response(
                (
                    "validate",
                    name,
                    {
                        "citation_ids": [source["source_ref"]],
                        "duration_ms": source["target_duration_ms"],
                    },
                )
            )
            selected.append(name)
        else:
            proof = history[-1]["result"]
            assert proof["valid"]
            value = {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "COMPLETE",
                                    "result": {
                                        "summary": "保留纸船故事因果，候选稿待人工评审。",
                                        "citation_ids": proof["citation_ids"],
                                        "duration_ms": proof["duration_ms"],
                                    },
                                    "human_request": None,
                                }
                            )
                        },
                    }
                ]
            }
        value["usage"] = {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"}
        return httpx.Response(200, json=value)

    model_service(respond)

    class RuntimeAdapter:
        agent_role = AgentRole.SCREENWRITER

        async def evaluate_case(self, selected_case):
            started = time.monotonic()
            outcome = run_agent_loop(
                client.app.state.session_factory,
                settings,
                model_request(
                    run_id, messages=[{"role": "user", "data": selected_case.input_payload}]
                ),
                registry,
                EvaluatedDelivery,
            )
            assert outcome.status == "COMPLETED"
            with client.app.state.session_factory.begin() as db:
                run = db.get(WorkflowRun, run_id)
                executions = list(
                    db.scalars(
                        select(AgentToolInvocation).where(
                            AgentToolInvocation.run_id == run_id,
                            AgentToolInvocation.status == "SUCCEEDED",
                        )
                    )
                )
                receipts = []
                for call in db.scalars(
                    select(GatewayCall).where(GatewayCall.workflow_id == run_id)
                ):
                    initial = latest_receipt(db, call.id)
                    assert initial.actual_cost is None  # Vendor JSON is not a billing receipt.
                    # Explicit synthetic settlement: this transport runs only local test code.
                    receipts.append(
                        reconcile(
                            db,
                            call,
                            actor,
                            "SUCCEEDED",
                            GatewayUsage.model_validate(initial.usage_json).model_copy(
                                update={"actual_cost": Decimal("0")}
                            ),
                            "offline-runtime-fixture: local MockTransport, no upstream requests",
                        )
                    )
                # Semantic fixture names map to server registrations; no expected output is copied.
                aliases = {
                    "story_read_version": "story.read_version",
                    "citation_validate": "citation.validate",
                }
                return EvaluationObservation(
                    case_id=selected_case.case_id,
                    sample_id=selected_case.sample_id,
                    agent_role=self.agent_role,
                    execution=ExecutionMetadata(
                        run_id=run_id,
                        trace_id=run.trace_id,
                        model_id=settings.llm_model,
                        model_version=settings.llm_agent_capabilities.version,
                        prompt_version="prompt:1",
                        workflow_version=run.workflow_version,
                        usage=UsageObservation(
                            input_tokens=sum(row.usage_json["input_tokens"] for row in receipts),
                            output_tokens=sum(row.usage_json["output_tokens"] for row in receipts),
                            cost_usd=sum((row.actual_cost for row in receipts), Decimal("0")),
                        ),
                    ),
                    schema_valid=True,
                    required_fields_present=True,
                    selected_tool_ids=tuple(aliases[name] for name in selected),
                    executed_tool_ids=tuple(aliases[row.tool_name] for row in executions),
                    citation_ids=tuple(outcome.result.citation_ids),
                    output_duration_ms=outcome.result.duration_ms,
                    decision=ExecutionDecision.COMPLETE,
                    latency_ms=max(1, int((time.monotonic() - started) * 1000)),
                )

    report = asyncio.run(
        EvaluationRunner().run(
            dataset,
            {AgentRole.SCREENWRITER: RuntimeAdapter()},
            case_ids=[case.case_id],
        )
    )
    report_path = tmp_path / "runtime-evaluation.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    result = report.cases[0]
    assert result.status == CaseStatus.PENDING_HUMAN_REVIEW
    assert set(result.failure_codes) == {FailureCode.HUMAN_REVIEW_REQUIRED}
    assert all(score.passed is not False for score in result.scores)
    assert result.execution.usage.input_tokens == 60
    assert result.execution.usage.output_tokens == 30
    assert result.execution.usage.cost_usd == 0
    assert len(rows_for(client, run_id)["actions"]) == 5
    assert case.input_payload["story"] not in report_path.read_text(encoding="utf-8")
