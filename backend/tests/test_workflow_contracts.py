"""FLOW-04: public contract compatibility, authoritative receipts and isolation."""

from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_api_workflow import advance, start
from test_collaboration import create_episode, create_project, register
from test_generation_budget import set_policy
from test_model_gateway import request, route

from arclight.application.gateway_accounting import reconcile
from arclight.application.model_gateway import admit, invoke
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayPolicy, GatewayUsage
from arclight.domain.workflow_contracts import GatewayCallUsage, WorkflowRunContract
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt


def contract_fixture(family="TEXT"):
    project_id, episode_id = str(uuid4()), str(uuid4())
    return {
        "schema_version": 1,
        "run_id": str(uuid4()),
        "project_id": project_id,
        "episode_id": episode_id,
        "requested_by": "service-owner",
        "task_type": {
            "TEXT": "script_storyboard",
            "ART": "art_direction",
            "REVIEW": "continuity_review",
            "MEDIA": "IMAGE",
        }[family],
        "requested_at": "2026-11-04T09:00:00+08:00",
        "started_at": "2026-11-04T01:00:01Z",
        "finished_at": "2026-11-04T01:00:02Z",
        "retry_count": 1,
        "input_snapshot": {
            "origin": "VERSION_BINDING",
            "sha256": "a" * 64,
            "references": [{"field": "asset_version_id", "value": str(uuid4())}],
            "context_manifest_ids": [str(uuid4())],
        },
        "workflow_version": "1.0",
        "versions": {
            "state": "BOUND",
            "workflow_version": "1.0",
            "model": {"provider": "fake", "model": "contract-model", "supplier": "fixture"},
            "parameter_sha256": "c" * 64,
            "components": [
                {
                    "id": str(uuid4()),
                    "family": family,
                    "kind": kind,
                    "name": "contract-fixture",
                    "version": "1.0",
                    "digest": "b" * 64,
                }
                for kind in ("AGENT", "PROMPT", "SCHEMA", "MODEL")
            ],
        },
        "budget": {
            "origin": "STARTUP_CONTEXT",
            "limit": "10",
            "task_limit": "2",
            "episode_limit": "5",
            "cost_estimate": "1.25",
        },
        "usage": {"total_calls": 0, "calls": []},
        "trace_id": "contract-fixture-trace",
        "status": "SUCCEEDED",
        "result": {
            "result_id": str(uuid4()),
            "project_id": project_id,
            "episode_id": episode_id,
            "agent_name": family.lower() + "_agent",
            "schema_version": "1.0",
            "status": "SUCCEEDED",
            "summary": "已登记候选结果",
            "artifact_ids": [str(uuid4())],
            "warnings": ["REVIEW_RECOMMENDED"],
            "requires_human_review": True,
        },
    }


@pytest.mark.parametrize("family", ["TEXT", "ART", "REVIEW", "MEDIA"])
def test_common_envelopes_round_trip_for_all_production_families(family):
    value = WorkflowRunContract.model_validate(contract_fixture(family))
    assert value.requested_at.isoformat() == "2026-11-04T01:00:00+00:00"
    assert WorkflowRunContract.model_validate_json(value.model_dump_json()) == value
    assert {component.family for component in value.versions.components} == {family}
    assert value.result.requires_human_review is True


def test_persisted_continuity_trace_keeps_its_original_namespace():
    payload = contract_fixture("REVIEW")
    payload["trace_id"] = "continuity:" + str(uuid4())
    value = WorkflowRunContract.model_validate(payload)
    assert value.trace_id == payload["trace_id"]


@pytest.mark.parametrize("trace", ["x y", "x/y", "x\ny", "x" * 65])
def test_trace_namespace_does_not_allow_payloads_paths_or_unbounded_values(trace):
    payload = contract_fixture("REVIEW") | {"trace_id": trace}
    with pytest.raises(ValidationError):
        WorkflowRunContract.model_validate(payload)


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), 2),
        (("schema_version",), True),
        (("run_id",), "not-a-uuid"),
        (("status",), "COMPLETED"),
        (("task_type",), ""),
        (("retry_count",), -1),
        (("retry_count",), True),
        (("requested_by",), "Bearer private-value"),
        (("versions", "workflow_version"), "other"),
        (("versions", "parameter_sha256"), None),
        (("versions", "model", "model"), "Bearer private-value"),
        (("versions", "components", 0, "kind"), "UNKNOWN"),
        (("versions", "components", 0, "family"), "UNKNOWN"),
        (("versions", "components", 0, "definition"), {"system_prompt": "private"}),
        (("budget", "cost_estimate"), "-1"),
        (("budget", "limit"), "NaN"),
        (("usage", "total_calls"), "0"),
        (("input_snapshot", "references", 0, "value"), "private-inline-data"),
        (("input_snapshot", "references", 0, "field"), "api_key"),
        (("input_snapshot", "references", 0, "value"), "a" * 64),
        (("input_snapshot", "context_manifest_ids"), ["wrong"]),
        (("result", "schema_version"), "2.0"),
        (("result", "status"), "FAILED"),
        (("result", "project_id"), "00000000-0000-0000-0000-000000000001"),
        (("result", "episode_id"), "00000000-0000-0000-0000-000000000002"),
        (("result", "artifact_ids"), ["https://private.invalid/image.png"]),
        (("result", "summary"), "s" * 501),
        (("result", "summary"), "Bearer private-value"),
        (("result", "summary"), "data:image/png;base64,AAAA"),
        (("result", "agent_name"), "sk-private1234567890"),
        (("result", "warnings"), ["private prose instead of a warning code"]),
        (("result", "requires_human_review"), "false"),
        (("finished_at",), "2026-11-03T01:00:00Z"),
        (("input_json",), {"api_key": "private"}),
    ],
)
def test_common_contract_rejects_invalid_versions_scopes_types_and_private_payloads(path, value):
    payload = contract_fixture()
    container = payload
    for field in path[:-1]:
        container = container[field]
    container[path[-1]] = value
    with pytest.raises(ValidationError):
        WorkflowRunContract.model_validate(payload)


@pytest.mark.parametrize("field", ["artifacts", "components", "references", "manifests"])
def test_contract_rejects_duplicate_reference_evidence(field):
    payload = contract_fixture()
    values = {
        "artifacts": payload["result"]["artifact_ids"],
        "components": payload["versions"]["components"],
        "references": payload["input_snapshot"]["references"],
        "manifests": payload["input_snapshot"]["context_manifest_ids"],
    }[field]
    values.append(deepcopy(values[0]))
    with pytest.raises(ValidationError):
        WorkflowRunContract.model_validate(payload)


def test_contract_rejects_unbounded_evidence_and_fabricated_legacy_versions():
    payload = contract_fixture()
    payload["input_snapshot"]["references"] *= 4097
    with pytest.raises(ValidationError):
        WorkflowRunContract.model_validate(payload)
    payload = contract_fixture()
    payload["versions"]["state"] = "LEGACY_UNBOUND"
    with pytest.raises(ValidationError):
        WorkflowRunContract.model_validate(payload)


def test_unresolved_gateway_usage_cannot_claim_zero_or_final_cost():
    payload = {
        "call_id": str(uuid4()),
        "task": "script",
        "provider": "fake",
        "model": "fixture",
        "status": "RESULT_UNKNOWN",
        "outcome_source": "INITIAL",
        "quoted_cost": None,
        "usage": {},
    }
    assert GatewayCallUsage.model_validate(payload).usage.actual_cost is None
    payload["usage"] = {"actual_cost": "0"}
    with pytest.raises(ValidationError):
        GatewayCallUsage.model_validate(payload)
    payload.update(status="SUCCEEDED", outcome_source="MISSING")
    with pytest.raises(ValidationError):
        GatewayCallUsage.model_validate(payload)


def test_run_contract_reads_latest_reconciliation_once_without_disclosing_evidence(client, episode):
    run_id = UUID(start(client, episode).json()["run_id"])
    selected = route(maximum_cost=None)
    call_request = request(episode, selected, task="script", workflow_id=run_id)
    factory = client.app.state.session_factory

    def timeout():
        raise TimeoutError("private-provider-error")

    with pytest.raises(DomainError):
        invoke(factory, call_request, selected, timeout, policy=GatewayPolicy())
    response = client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    usage = response.json()["contract"]["usage"]
    assert usage["total_calls"] == 1
    assert usage["calls"][0]["status"] == "RESULT_UNKNOWN"
    assert usage["calls"][0]["quoted_cost"] is None
    assert usage["calls"][0]["usage"]["actual_cost"] is None
    evidence = "private supplier reconciliation evidence flow04"
    for _ in range(2):
        with factory.begin() as db:
            call = db.scalar(select(GatewayCall).where(GatewayCall.workflow_id == run_id))
            reconcile(
                db,
                call,
                "service-owner",
                "SUCCEEDED",
                GatewayUsage(input_tokens=10, output_tokens=20, actual_cost=Decimal("0.125")),
                evidence,
            )
    response = client.get(f"/api/v1/runs/{run_id}")
    usage = response.json()["contract"]["usage"]
    assert usage["total_calls"] == 1 and len(usage["calls"]) == 1
    receipt = usage["calls"][0]
    assert receipt["status"] == "SUCCEEDED" and receipt["outcome_source"] == "RECONCILIATION"
    assert Decimal(receipt["usage"]["actual_cost"]) == Decimal("0.125")
    assert receipt["usage"]["input_tokens"] == 10
    assert evidence not in response.text and "private-provider-error" not in response.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(GatewayReceipt)) == 2


def test_run_budget_snapshot_does_not_change_with_live_project_limits(client, episode):
    project_id = episode["project_id"]
    set_policy(client, project_id, "20", task_limit="4", episode_limit="8")
    run_id = start(client, episode).json()["run_id"]
    frozen = client.get(f"/api/v1/runs/{run_id}").json()["contract"]["budget"]
    assert frozen["origin"] == "STARTUP_CONTEXT"
    assert [Decimal(frozen[field]) for field in ("limit", "task_limit", "episode_limit")] == [
        Decimal("20"),
        Decimal("4"),
        Decimal("8"),
    ]
    assert Decimal(frozen["cost_estimate"]) == 0
    set_policy(client, project_id, "30", task_limit="6", episode_limit="12")
    assert client.get(f"/api/v1/runs/{run_id}").json()["contract"]["budget"] == frozen


def test_call_without_receipt_keeps_outcome_and_actual_usage_unknown(client, episode):
    run_id = UUID(start(client, episode).json()["run_id"])
    selected = route(maximum_cost=Decimal("0"))
    call_request = request(episode, selected, task="script", workflow_id=run_id)
    with client.app.state.session_factory.begin() as db:
        admit(db, call_request, selected, GatewayPolicy())
    response = client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    usage = response.json()["contract"]["usage"]
    assert usage["total_calls"] == 1
    assert usage["calls"][0]["status"] == "RESULT_UNKNOWN"
    assert usage["calls"][0]["outcome_source"] == "MISSING"
    assert usage["calls"][0]["usage"]["actual_cost"] is None


def test_contract_access_respects_membership_revocation_and_original_caller(client, settings):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    run_id = start(client, episode).json()["run_id"]
    owner_contract = client.get(f"/api/v1/runs/{run_id}").json()["contract"]
    assert owner_contract["requested_by"] == owner["user"]["id"]
    member = register(client, "contract-member@example.com")
    other_project = create_project(client)
    other_episode = create_episode(client, other_project)
    other_run = start(client, other_episode).json()["run_id"]
    assert client.get(f"/api/v1/runs/{run_id}").status_code == 404
    assert client.get(f"/api/v1/episodes/{episode['id']}/runs").status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    member_url = f"/api/v1/projects/{project['id']}/members"
    response = client.post(member_url, json={"email": member["user"]["email"], "role": "viewer"})
    assert response.status_code == 201, response.text
    for role in ("viewer", "editor"):
        client.headers["Authorization"] = "Bearer " + owner["access_token"]
        response = client.patch(member_url + "/" + member["user"]["id"], json={"role": role})
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = "Bearer " + member["access_token"]
        assert client.get(f"/api/v1/runs/{run_id}").json()["contract"] == owner_contract
        assert client.get(f"/api/v1/episodes/{episode['id']}/runs").json()[0]["contract"]
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert client.get(f"/api/v1/runs/{other_run}").status_code == 404
    assert client.delete(member_url + "/" + member["user"]["id"]).status_code == 204
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.get(f"/api/v1/runs/{run_id}").status_code == 404
    assert client.get(f"/api/v1/episodes/{episode['id']}/runs").status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert advance(client, settings, run_id)["contract"]["requested_by"] == owner["user"]["id"]
