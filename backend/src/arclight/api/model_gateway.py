"""Project-scoped diagnostics, Owner pause and explicit evidence reconciliation."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field, field_validator
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.gateway_accounting import latest_receipt, media_outcome, reconcile
from arclight.application.model_gateway import lock_gate
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayUsage
from arclight.persistence.model_gateway import GatewayCall, GatewayCircuit

router = APIRouter(prefix="/api/v1/projects/{project_id}/model-gateway", tags=["model-gateway"])


class PauseInput(Input):
    expected_revision: int = Field(ge=0)
    paused: bool
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        from arclight.domain.generation_requests import safe_metadata

        safe_metadata(value)
        return value


class ReconcileInput(Input):
    status: Literal["SUCCEEDED", "FAILED", "NOT_ACCEPTED"]
    usage: GatewayUsage
    evidence: str = Field(min_length=1, max_length=1000)

    @field_validator("evidence")
    @classmethod
    def safe_evidence(cls, value: str) -> str:
        from arclight.domain.generation_requests import safe_metadata

        safe_metadata(value)
        return value


@router.get("/calls")
def calls(
    project_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[dict]:
    require_project(db, project_id, actor)
    result = []
    for call in db.scalars(
        select(GatewayCall)
        .where(GatewayCall.project_id == project_id)
        .order_by(GatewayCall.created_at, GatewayCall.id)
        .limit(limit)
        .offset(offset)
    ):
        receipt = latest_receipt(db, call.id)
        result.append(
            {
                "id": str(call.id),
                "schema_version": 1,
                "workflow_id": str(call.workflow_id) if call.workflow_id else None,
                "generation_attempt_id": str(call.generation_attempt_id)
                if call.generation_attempt_id
                else None,
                "task": call.task,
                "sequence": call.sequence,
                "actor": call.actor,
                "input_sha256": call.input_sha256,
                "route": call.route_json,
                "parameters": call.parameters_json,
                "circuit_id": call.circuit_id,
                "created_at": call.created_at,
                "maximum_cost": format(call.maximum_cost, ".6f")
                if call.maximum_cost is not None
                else None,
                "status": receipt.status if receipt else "RESULT_UNKNOWN",
                "error_code": receipt.error_code if receipt else None,
                "usage": receipt.usage_json if receipt else {},
                "cost_is_estimate": receipt is None or receipt.actual_cost is None,
                "outcome_source": "GATEWAY_RECEIPT",
                **(media_outcome(db, call) or {}),
            }
        )
    return result


@router.get("/circuits")
def circuits(project_id: UUID, db: Database, actor: Actor) -> list[dict]:
    require_project(db, project_id, actor)
    return [
        {
            "id": row.id,
            "revision": row.revision,
            "paused": row.paused,
            "failures": row.failures,
            "opened_until": row.opened_until,
        }
        for row in db.scalars(select(GatewayCircuit).where(GatewayCircuit.project_id == project_id))
    ]


@router.patch("/circuits/{circuit_id}")
def pause(
    project_id: UUID, circuit_id: str, payload: PauseInput, db: Database, actor: Actor
) -> dict:
    from arclight.domain.generation_requests import safe_metadata

    lock_gate(db)
    require_project(db, project_id, actor, owner=True, write=True)
    safe_metadata(payload.reason)
    row = db.get(GatewayCircuit, circuit_id, populate_existing=True)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "模型路由不存在", 404)
    if row.revision != payload.expected_revision:
        raise DomainError("VERSION_CONFLICT", "模型路由状态已更新", 409)
    row.paused = payload.paused
    row.revision += 1
    audit(
        db,
        project_id,
        actor,
        "model_gateway.pause_changed",
        project_id,
        {
            "circuit_id": row.id,
            "paused": row.paused,
            "reason": payload.reason,
            "revision": row.revision,
        },
    )
    return {"id": row.id, "revision": row.revision, "paused": row.paused}


@router.post("/calls/{call_id}/reconcile")
def reconcile_call(
    project_id: UUID, call_id: UUID, payload: ReconcileInput, db: Database, actor: Actor
) -> dict:
    lock_gate(db)
    require_project(db, project_id, actor, owner=True, write=True)
    call = db.get(GatewayCall, call_id)
    if call is None or call.project_id != project_id:
        raise DomainError("NOT_FOUND", "模型调用不存在", 404)
    row = reconcile(db, call, actor, payload.status, payload.usage, payload.evidence)
    return {
        "id": str(row.id),
        "call_id": str(row.call_id),
        "status": row.status,
        "usage": row.usage_json,
        "evidence": row.evidence,
    }
