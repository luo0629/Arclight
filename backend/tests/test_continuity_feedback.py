"""Feedback uses actual persistent reviews and the sole media revision service."""

import json
from uuid import UUID

import httpx
import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_agent_runtime import final
from test_asset_selection import choose
from test_asset_versions import state
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_revisions import execution_counts, request_revision, revision_job
from test_continuity_revisions import revision_case as revision_case
from test_continuity_rules import rules_storage as rules_storage
from test_generation_candidates import candidates, ingest
from test_generation_delivery import deliver
from test_human_reviews import decide, decision_payload, resume

from arclight.application.continuity_feedback import review_revision_feedback
from arclight.application.continuity_feedback_store import (
    feedback_history,
    validate_feedback_record,
)
from arclight.application.continuity_revision_store import revision_record
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import AssetVersion, GenerationAttempt, VisualAsset
from arclight.providers.media_submission import MockMediaSubmissionProvider


def finish_revision(client, episode, case, revision):
    job = revision_job(client, case, revision)
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 1
    rows = candidates(client, episode["project_id"], job)
    assert len(rows) == 1 and rows[0]["media_check"]["status"] == "PASSED"
    return rows[0]


@pytest.fixture
def feedback_case(client, episode, revision_case):
    response = request_revision(client, revision_case)
    assert response.status_code == 201, response.text
    revision = response.json()
    candidate = finish_revision(client, episode, revision_case, revision)
    return revision_case | {"revision": revision, "candidate": candidate}


def feedback_seed(payload):
    for message in payload["messages"]:
        if message["role"] == "user" and isinstance(message.get("content"), str):
            data = json.loads(message["content"]).get("data", {})
            if data.get("schema_version") == "continuity-feedback-seed/1":
                return data
    return None


def install_feedback_model(
    model_service, actions, *, difference=None, uncertainty=None, mutate=None, on_feedback=None
):
    handlers, sent, seeds = [], [], []
    reviewed, errors, deliveries = install_reviewer_model(
        handlers.append, dynamic=False, difference=difference, uncertainty=uncertainty
    )

    def respond(request):
        payload = json.loads(request.content)
        seed = feedback_seed(payload)
        if seed is None:
            return handlers[0](request)
        sent.append(payload)
        seeds.append(seed)
        if on_feedback:
            on_feedback(seed)
        action = actions[min(len(sent) - 1, len(actions) - 1)]
        if action == "ASK":
            value = final("HUMAN_REVIEW")
            value["usage"]["actual_cost"] = "0"
            return httpx.Response(200, json=value)
        decision = {
            "decision_id": seed["decision_id"],
            "revision_id": seed["revision"]["revision_id"],
            "review_id": seed["review"]["review"]["result_id"],
            "review_sha256": seed["review_sha256"],
            "action": action,
            "reason": "Compare the red case state with the exact references once more.",
        }
        if action == "REVISE":
            decision["revision_request"] = {
                "review_id": decision["review_id"],
                "issue_ids": [seed["review"]["review"]["issues"][0]["issue_id"]],
                "action": "REGENERATE",
                "amendments": [],
            }
        if mutate:
            mutate(decision)
        value = final(result=decision)
        value["usage"]["actual_cost"] = "0"
        return httpx.Response(200, json=value)

    model_service(respond)
    return sent, seeds, reviewed, errors, deliveries


def run_feedback(client, settings, case, **kwargs):
    return review_revision_feedback(
        client.app.state.session_factory,
        settings,
        case["run_id"],
        case["actor"],
        UUID(case["revision"]["revision_id"]),
        **kwargs,
    )


def feedback_rows(client, case):
    with client.app.state.session_factory() as db:
        return feedback_history(db, UUID(case["project_id"]), UUID(case["revision"]["revision_id"]))


def target_rows(client, case):
    root = f"/api/v1/projects/{case['project_id']}/assets/{case['target']['asset_id']}"
    asset = client.get(root)
    version = client.get(root + f"/versions/{case['candidate']['asset_version_id']}")
    assert asset.status_code == version.status_code == 200
    return asset.json(), version.json()


def test_new_image_is_actually_reviewed_before_feedback_without_selecting(
    client, settings, model_service, feedback_case
):
    case = feedback_case
    sent, seeds, reviewed, errors, deliveries = install_feedback_model(
        model_service, ["READY_FOR_REVIEW"]
    )
    before = execution_counts(client)
    result = run_feedback(client, settings, case)
    assert result.status == "READY_FOR_REVIEW", result.model_dump_json()
    assert not errors and len(deliveries) == len(sent) == 1 and len(reviewed) >= 5
    record = result.record
    assert record.target.candidate_id == UUID(case["candidate"]["id"])
    assert record.target.asset_version_id != case["review"].target.asset_version_id
    assert seeds[0]["previous_review"]["review"] == case["review"].model_dump(mode="json")
    assert seeds[0]["review"]["review"]["conclusion"] == "PASS"
    assert any(isinstance(m.get("content"), list) for p in reviewed for m in p["messages"])
    after = execution_counts(client)
    for table in ("generation_jobs", "generation_attempts", "generation_budget_reservations"):
        assert after[table] == before[table]
    with client.app.state.session_factory() as db:
        validate_feedback_record(db, record)
        assert db.get(AssetVersion, record.target.asset_version_id).status == "DRAFT"
        assert db.get(VisualAsset, record.target.asset_id).current_version_id is None
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 1
        loops = list(db.scalars(select(AgentLoop)))
        assert len({row.run_id for row in loops}) == 1
    assert run_feedback(client, settings, case) == result
    assert len(sent) == 1 and len(deliveries) == 1
    response = client.get(
        f"/api/v1/projects/{case['project_id']}/continuity-revisions/"
        f"{case['revision']['revision_id']}/feedback"
    )
    assert response.status_code == 200, response.text
    assert response.json() == [record.model_dump(mode="json")]
    asset, version = target_rows(client, case)
    assert choose(client, asset, version).status_code == 409


def test_feedback_revision_uses_qa05_once_then_new_candidate_requires_new_review(
    client, episode, settings, model_service, feedback_case
):
    case = feedback_case
    sent, _, _, errors, _ = install_feedback_model(
        model_service, ["REVISE"], difference=("PROP", "MEDIUM")
    )
    before = execution_counts(client)
    result = run_feedback(client, settings, case)
    assert result.status == "REVISION_QUEUED", result.model_dump_json()
    assert not errors and len(sent) == 1
    after = execution_counts(client)
    assert after["generation_jobs"] == before["generation_jobs"] + 1
    assert after["generation_attempts"] == before["generation_attempts"]
    assert run_feedback(client, settings, case) == result
    assert execution_counts(client) == after
    with client.app.state.session_factory() as db:
        child = revision_record(db, UUID(case["project_id"]), result.record.child_revision_id)
        assert child.parent_revision_id == UUID(case["revision"]["revision_id"])
        assert child.round == 2 and child.request == result.record.decision.revision_request
    candidate = finish_revision(client, episode, case, child.model_dump(mode="json"))
    new_case = case | {"revision": child.model_dump(mode="json"), "candidate": candidate}
    asset, version = target_rows(client, new_case)
    response = state(client, asset, version, "READY_FOR_REVIEW")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"


def test_additional_evidence_rechecks_same_candidate_on_same_limits(
    client, settings, model_service, feedback_case
):
    case = feedback_case
    sent, seeds, reviewed, errors, deliveries = install_feedback_model(
        model_service, ["INVESTIGATE", "READY_FOR_REVIEW"]
    )
    result = run_feedback(client, settings, case)
    assert result.status == "READY_FOR_REVIEW", result.model_dump_json()
    assert not errors and len(sent) == len(deliveries) == 2
    history = feedback_rows(client, case)
    assert [row.sequence for row in history] == [1, 2]
    assert history[0].target == history[1].target
    assert history[0].review_id != history[1].review_id
    assert seeds[1]["history"] == [history[0].model_dump(mode="json")]
    assert seeds[1]["review"]["review"]["rerun"]["previous_review_id"] == str(history[0].review_id)
    assert seeds[1]["remaining_decisions"] == 2
    assert seeds[0]["context_evidence_start"] < seeds[1]["context_evidence_start"]
    fresh = []
    for payload in reviewed:
        memory = [
            json.loads(message["content"]).get("data", {})
            for message in payload["messages"]
            if message["role"] == "user" and isinstance(message.get("content"), str)
        ]
        observations = [item["observation"] for item in memory if "observation" in item]
        if not any(message["role"] == "tool" for message in payload["messages"]) and observations:
            fresh.append(observations[0])
    assert len(fresh) == 2
    assert all(item["evidence"] == [] and item["startup"] is not None for item in fresh)
    assert fresh[0]["task_context"] == fresh[1]["task_context"]
    from arclight.application.context_evidence import read_trace
    from arclight.persistence.models import WorkflowRun

    with client.app.state.session_factory() as db:
        trace = read_trace(db, db.get(WorkflowRun, case["run_id"]), "storyboard", case["actor"])
        assert len(trace["evidence"]) == seeds[1]["context_evidence_start"]
        assert trace["evidence"][0]["round"] == 1
    assert run_feedback(client, settings, case) == result


@pytest.mark.parametrize("answer", ["APPROVE", "REJECT"])
def test_model_human_choice_uses_shared_exact_owner_handling(
    client, settings, model_service, feedback_case, answer
):
    case = feedback_case
    sent, _, _, errors, _ = install_feedback_model(model_service, ["HUMAN_REVIEW"])
    result = run_feedback(client, settings, case)
    assert result.status == "HUMAN_REVIEW", result.model_dump_json()
    assert not errors
    asset, version = target_rows(client, case)
    blocked = state(client, asset, version, "READY_FOR_REVIEW")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "HUMAN_DECISION_REQUIRED"
    request = client.get(f"/api/v1/human-reviews/{result.record.human_review_request_id}")
    assert request.status_code == 200, request.text
    row = request.json()
    blank = client.post(
        f"/api/v1/human-reviews/{row['id']}/decisions",
        json=decision_payload(row, answer, "  "),
    )
    assert blank.status_code == 422, blank.text
    decision = decide(client, row, answer)
    assert decision.status_code == 200, decision.text
    resumed = resume(client, row, decision.json())
    assert resumed.status_code == 200, resumed.text
    assert resume(client, row, decision.json()).json() == resumed.json()
    asset, version = target_rows(client, case)
    assert asset["current_version_id"] is None and version["status"] == "DRAFT"
    response = state(client, asset, version, "READY_FOR_REVIEW")
    assert response.status_code == (200 if answer == "APPROVE" else 409), response.text
    assert len(sent) == 1


def test_feedback_cap_stops_with_durable_human_request_without_extra_model(
    client, settings, model_service, feedback_case
):
    case = feedback_case
    sent, _, _, errors, deliveries = install_feedback_model(model_service, ["INVESTIGATE"])
    result = run_feedback(client, settings, case)
    assert result.status == "HUMAN_REVIEW", result.model_dump_json()
    assert result.record.stop_code == "FEEDBACK_ROUND_LIMIT"
    assert len(sent) == len(deliveries) == 3 and not errors
    assert len(feedback_rows(client, case)) == 4
    assert run_feedback(client, settings, case) == result


@pytest.mark.parametrize("field", ["decision_id", "review_id", "review_sha256"])
def test_invented_feedback_cannot_create_revision_or_admission(
    client, settings, model_service, feedback_case, field
):
    from uuid import uuid4

    def mutate(decision):
        decision[field] = "0" * 64 if field.endswith("sha256") else str(uuid4())

    case = feedback_case
    install_feedback_model(model_service, ["READY_FOR_REVIEW"], mutate=mutate)
    before = execution_counts(client)
    result = run_feedback(client, settings, case)
    assert result.status == "STOPPED", result.model_dump_json()
    assert not feedback_rows(client, case)
    after = execution_counts(client)
    for table in ("generation_jobs", "generation_attempts", "generation_budget_reservations"):
        assert after[table] == before[table]
    asset, version = target_rows(client, case)
    blocked = state(client, asset, version, "READY_FOR_REVIEW")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "CONTINUITY_FEEDBACK_PENDING"
