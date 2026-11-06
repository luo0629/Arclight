"""FLOW-03 real text integration and controlled future-domain review contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from test_api_workflow import advance, review, start
from test_collaboration import register

from arclight.application import human_reviews
from arclight.application.human_reviews import (
    ReviewHandler,
    request_review,
    resume_review,
    submit_decision,
)
from arclight.core.errors import DomainError
from arclight.domain.human_reviews import HumanDecision, ReviewResume, ReviewSnapshot
from arclight.main import create_app
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import (
    AssetVersion,
    OutboxEvent,
    Project,
    ProjectMember,
    Review,
    Script,
    User,
    WorkflowRun,
)
from arclight.providers.text import MockTextProvider


def count(client, model):
    with client.app.state.session_factory() as db:
        return db.scalar(select(func.count()).select_from(model))


def pending_review(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    rows = client.get(f"/api/v1/projects/{episode['project_id']}/human-reviews").json()
    assert len(rows) == 1, rows
    return state, rows[0]


def decision_payload(row, decision="APPROVE", comment="同意当前精确版本"):
    return {
        "version_id": row["snapshot"]["version_id"],
        "request_sha256": row["request_sha256"],
        "decision": decision,
        "comment": comment,
    }


def decide(client, row, decision="APPROVE"):
    return client.post(
        f"/api/v1/human-reviews/{row['id']}/decisions", json=decision_payload(row, decision)
    )


def resume(client, row, saved):
    return client.post(
        f"/api/v1/human-reviews/{row['id']}/resume",
        json={"decision_id": saved["id"], "request_sha256": row["request_sha256"]},
    )


@pytest.fixture
def contract_registry(monkeypatch):
    """Future producers have no real domain tables yet; use explicit database contract facts.

    This adapter is never installed in the app. Production safely refuses unknown handlers.
    The fixture models version publication independently from the immutable review journal.
    """

    def read(db, frozen):
        project = db.get(Project, frozen.project_id, populate_existing=True)
        current = project.output_profile["review_contracts"][str(frozen.object_id)]
        return ReviewSnapshot.model_validate(current)

    def apply(db, row, decision, actor):
        project = db.get(Project, row.project_id, populate_existing=True)
        profile = dict(project.output_profile)
        profile["applied_reviews"] = profile.get("applied_reviews", 0) + 1
        project.output_profile = profile
        db.flush()
        if profile.get("fail_review_apply"):
            raise DomainError("CONTRACT_APPLY_FAILED", "控制合同模拟事务后半段失败", 409)
        return {
            "status": "APPROVED" if decision.decision_json["decision"] == "APPROVE" else "REJECTED"
        }

    registry = {"CONTROLLED_CONTRACT": ReviewHandler(read, apply)}
    monkeypatch.setattr(human_reviews, "handlers", lambda: registry)
    return registry


def contract_review(
    client,
    episode,
    kind="REFERENCE",
    purpose="PRODUCTION",
    actor="service-owner",
    *,
    run_id=None,
    loop_id=None,
):
    identity = uuid4()
    snapshot = ReviewSnapshot.model_validate(
        {
            "project_id": episode["project_id"],
            "episode_id": episode["id"],
            "run_id": run_id,
            "loop_id": loop_id,
            "thread_id": f"agent-loop:{loop_id}"
            if loop_id
            else str(run_id)
            if run_id
            else "review:" + str(identity),
            "kind": kind,
            "purpose": purpose,
            "object_id": identity,
            "version_id": uuid4(),
            "content_sha256": "a" * 64,
            "input_sha256": "b" * 64,
            "source_version_ids": [uuid4()],
        }
    )
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, snapshot.project_id)
        project.output_profile = {
            **project.output_profile,
            "review_contracts": {
                **project.output_profile.get("review_contracts", {}),
                str(identity): snapshot.model_dump(mode="json"),
            },
        }
        row = request_review(db, actor, str(identity), "CONTROLLED_CONTRACT", snapshot)
        request_id = row.id
    response = client.get(f"/api/v1/human-reviews/{request_id}")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("kind", ["REFERENCE", "VISUAL_SPEC", "EXCEPTIONAL_SHOT", "EXPORT"])
@pytest.mark.parametrize("decision", ["APPROVE", "REJECT"])
def test_independent_contract_review_persists_identity_opinion_and_once_only_resume(
    client, episode, settings, contract_registry, kind, decision
):
    row = contract_review(client, episode, kind)
    saved = decide(client, row, decision)
    assert saved.status_code == 200, saved.text
    value = saved.json()
    assert value["actor_id"] == "service-owner"
    assert value["decision_json"]["comment"] == "同意当前精确版本"
    assert datetime.fromisoformat(value["created_at"]).replace(tzinfo=UTC) <= datetime.now(UTC)
    assert decide(client, row, decision).json() == value
    assert decide(client, row, "REJECT" if decision == "APPROVE" else "APPROVE").status_code == 409
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        assert restarted.get(f"/api/v1/human-reviews/{row['id']}").json()["status"] == "DECIDED"
        first = resume(restarted, row, value)
        assert first.status_code == 200, first.text
        second = resume(restarted, row, value)
        assert second.json() == first.json()
        current = restarted.get(f"/api/v1/human-reviews/{row['id']}").json()
    assert current["snapshot"] == row["snapshot"]
    assert current["status"] == "RESUMED"
    assert first.json()["result_json"]["version_id"] == row["snapshot"]["version_id"]
    assert first.json()["result_json"]["status"] == (
        "APPROVED" if decision == "APPROVE" else "REJECTED"
    )
    with client.app.state.session_factory() as db:
        project = db.get(Project, UUID(episode["project_id"]))
        assert project.output_profile["applied_reviews"] == 1
    assert count(client, HumanReviewDecision) == count(client, HumanReviewResume) == 1
    assert count(client, Review) == count(client, OutboxEvent) == 0


@pytest.mark.parametrize("phase", ["DECISION", "RESUME"])
@pytest.mark.parametrize(
    "field", ["version_id", "input_sha256", "content_sha256", "source_version_ids"]
)
def test_changed_authoritative_contract_fact_cannot_receive_or_resume_old_decision(
    client, episode, contract_registry, phase, field
):
    row = contract_review(client, episode)
    saved = decide(client, row).json() if phase == "RESUME" else None
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(episode["project_id"]))
        profile = dict(project.output_profile)
        facts = dict(profile["review_contracts"])
        current = dict(facts[row["snapshot"]["object_id"]])
        current[field] = (
            str(uuid4())
            if field == "version_id"
            else [str(uuid4())]
            if field == "source_version_ids"
            else "c" * 64
        )
        facts[row["snapshot"]["object_id"]] = current
        project.output_profile = {**profile, "review_contracts": facts}
    response = decide(client, row) if saved is None else resume(client, row, saved)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "REVIEW_INPUT_CHANGED"
    assert count(client, HumanReviewResume) == 0
    assert count(client, HumanReviewDecision) == (1 if phase == "RESUME" else 0)


def test_preview_rejection_and_production_approval_are_separate_objects(
    client, episode, contract_registry
):
    preview = contract_review(client, episode, "EXPORT", "PREVIEW")
    formal = contract_review(client, episode, "EXPORT", "PRODUCTION")
    denied = decide(client, preview)
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "PREVIEW_APPROVAL_FORBIDDEN"
    rejected = decide(client, preview, "REJECT").json()
    assert resume(client, formal, rejected).status_code == 409
    assert resume(client, preview, rejected).status_code == 200
    assert client.get(f"/api/v1/human-reviews/{formal['id']}").json()["status"] == "WAITING_REVIEW"
    assert count(client, HumanReviewResume) == 1


def test_request_replay_cannot_change_version_handler_or_actor(client, episode, contract_registry):
    row = contract_review(client, episode)
    snapshot = ReviewSnapshot.model_validate(row["snapshot"])
    with client.app.state.session_factory.begin() as db:
        previous = request_review(
            db, "service-owner", str(snapshot.object_id), "CONTROLLED_CONTRACT", snapshot
        )
        assert str(previous.id) == row["id"]
    with pytest.raises(DomainError), client.app.state.session_factory.begin() as db:
        request_review(db, "service-owner", str(snapshot.object_id), "UNKNOWN_HANDLER", snapshot)
    assert count(client, HumanReviewRequest) == 1


def test_resume_transaction_failure_keeps_decision_and_rolls_back_effect_and_receipt(
    client, episode, contract_registry
):
    row = contract_review(client, episode)
    saved = decide(client, row).json()
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(episode["project_id"]))
        project.output_profile = {**project.output_profile, "fail_review_apply": True}
    failed = resume(client, row, saved)
    assert failed.status_code == 409
    with client.app.state.session_factory() as db:
        assert "applied_reviews" not in db.get(Project, UUID(episode["project_id"])).output_profile
    assert count(client, HumanReviewDecision) == 1 and count(client, HumanReviewResume) == 0
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(episode["project_id"]))
        project.output_profile = {**project.output_profile, "fail_review_apply": False}
    assert resume(client, row, saved).status_code == 200
    assert count(client, HumanReviewResume) == 1


@pytest.mark.parametrize("change", ["ARCHIVE_PROJECT", "ARCHIVE_EPISODE", "DELETE_PROJECT"])
def test_scope_lifecycle_rechecked_between_decision_and_resume(
    client, episode, contract_registry, change
):
    row = contract_review(client, episode)
    saved = decide(client, row).json()
    with client.app.state.session_factory.begin() as db:
        from arclight.persistence.models import Episode

        project = db.get(Project, UUID(episode["project_id"]))
        if change == "ARCHIVE_PROJECT":
            project.archived = True
        elif change == "DELETE_PROJECT":
            project.deleted_at = datetime.now(UTC)
        else:
            db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
    response = resume(client, row, saved)
    assert response.status_code == (404 if change == "DELETE_PROJECT" else 409)
    assert count(client, HumanReviewResume) == 0


def test_owner_rights_and_account_revocation_rechecked_even_for_saved_decisions(
    client, contract_registry
):
    owner = register(client)
    project = client.post("/api/v1/projects", json={"name": "审核权限"}).json()
    episode = client.post(
        f"/api/v1/projects/{project['id']}/episodes", json={"episode_no": 1, "title": "一"}
    ).json()
    owner_id = owner["user"]["id"]
    row = contract_review(client, episode, actor=owner_id)
    saved = decide(client, row).json()
    stranger = register(client, "reader@example.com")
    assert client.get(f"/api/v1/human-reviews/{row['id']}").status_code == 404
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(project["id"]), user_id=UUID(stranger["user"]["id"]), role="editor"
            )
        )
    assert client.get(f"/api/v1/human-reviews/{row['id']}").status_code == 200
    assert decide(client, row).status_code == 403
    assert resume(client, row, saved).status_code == 403
    with client.app.state.session_factory.begin() as db:
        project_row = db.get(Project, UUID(project["id"]))
        project_row.owner_id = stranger["user"]["id"]
    # A new Owner cannot execute an old Owner's now-revoked approval.
    assert resume(client, row, saved).status_code == 404
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(project["id"])).owner_id = owner_id
    with client.app.state.session_factory.begin() as db:
        db.get(User, UUID(owner_id)).disabled = True
    with pytest.raises(DomainError, match="不可用"), client.app.state.session_factory.begin() as db:
        submit_decision(
            db, UUID(row["id"]), owner_id, HumanDecision.model_validate(decision_payload(row))
        )
    with pytest.raises(DomainError, match="不可用"), client.app.state.session_factory.begin() as db:
        resume_review(
            db,
            UUID(row["id"]),
            owner_id,
            ReviewResume(decision_id=UUID(saved["id"]), request_sha256=row["request_sha256"]),
        )
    assert count(client, HumanReviewResume) == 0


def test_http_cannot_create_subjects_replace_current_facts_or_invent_decisions(
    client, episode, contract_registry
):
    row = contract_review(client, episode)
    assert (
        client.post(f"/api/v1/projects/{episode['project_id']}/human-reviews", json=row).status_code
        == 405
    )
    assert (
        client.patch(f"/api/v1/human-reviews/{row['id']}", json={"status": "RESUMED"}).status_code
        == 405
    )
    invalid = decision_payload(row) | {"current_snapshot": row["snapshot"]}
    assert (
        client.post(f"/api/v1/human-reviews/{row['id']}/decisions", json=invalid).status_code == 422
    )
    assert resume(client, row, {"id": str(uuid4())}).status_code == 409
    invalid = decision_payload(row) | {"version_id": str(uuid4())}
    assert (
        client.post(f"/api/v1/human-reviews/{row['id']}/decisions", json=invalid).status_code == 409
    )
    invalid = decision_payload(row) | {"request_sha256": "f" * 64}
    assert (
        client.post(f"/api/v1/human-reviews/{row['id']}/decisions", json=invalid).status_code == 409
    )
    assert count(client, HumanReviewDecision) == 0


def test_unconnected_domain_handler_fails_closed(client, episode, contract_registry, monkeypatch):
    row = contract_review(client, episode)
    monkeypatch.setattr(human_reviews, "handlers", lambda: {})
    response = decide(client, row)
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "REVIEW_HANDLER_UNAVAILABLE"
    )
    assert count(client, HumanReviewDecision) == 0


def test_text_decision_survives_restart_and_resumes_original_graph_once(client, episode, settings):
    state, row = pending_review(client, episode, settings)
    saved = decide(client, row)
    assert saved.status_code == 200, saved.text
    assert client.get(f"/api/v1/runs/{state['id']}").json()["status"] == "WAITING_REVIEW"
    before = count(client, OutboxEvent)
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        first = resume(restarted, row, saved.json())
        assert first.status_code == 200, first.text
        assert resume(restarted, row, saved.json()).json() == first.json()
        assert count(restarted, OutboxEvent) == before + 2  # workflow handoff + production event
        next_state = advance(restarted, settings, state["id"])
        assert next_state["pending_review"]["resource_type"] == "storyboard"
        assert review(restarted, next_state).status_code == 202
        assert advance(restarted, settings, state["id"])["status"] == "SUCCEEDED"
    assert first.json()["result_json"]["thread_id"] == state["id"]
    assert (
        count(client, HumanReviewDecision)
        == count(client, HumanReviewResume)
        == count(client, Review)
        == 2
    )
    assert count(client, Script) == 1


@pytest.mark.parametrize("change", ["CANCEL", "STALE"])
def test_text_cancel_or_stale_between_decision_and_resume_blocks_handoff(
    client, episode, settings, change
):
    state, row = pending_review(client, episode, settings)
    saved = decide(client, row).json()
    if change == "CANCEL":
        assert client.post(f"/api/v1/runs/{state['id']}/cancel").status_code == 200
    else:
        with client.app.state.session_factory.begin() as db:
            db.get(AssetVersion, UUID(row["snapshot"]["version_id"])).stale = True
    response = resume(client, row, saved)
    assert response.status_code == 409, response.text
    blocked = client.get(f"/api/v1/human-reviews/{row['id']}").json()
    assert blocked["status"] == "BLOCKED" and blocked["blocking_code"] == "REVIEW_INPUT_CHANGED"
    assert count(client, HumanReviewResume) == count(client, Review) == 0


def test_manual_text_uses_same_gate_and_journal_without_starting_workflow(client, episode):
    content = MockTextProvider().script("雨夜的信", {}).model_dump(mode="json")
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/scripts",
        json={"expected_version_no": 0, "content": content},
    )
    assert response.status_code == 201, response.text
    rows = client.get(f"/api/v1/projects/{episode['project_id']}/human-reviews").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["snapshot"]["run_id"] is None
    saved = decide(client, row).json()
    assert resume(client, row, saved).status_code == 200
    versions = client.get(f"/api/v1/episodes/{episode['id']}/scripts").json()
    assert (
        next(item for item in versions if item["id"] == response.json()["id"])["status"]
        == "APPROVED"
    )
    assert count(client, WorkflowRun) == count(client, OutboxEvent) == 0
    assert count(client, HumanReviewResume) == 1


@pytest.mark.parametrize("operation", ["DECISION", "RESUME"])
def test_postgres_parallel_decision_or_resume_has_single_effect(
    client, episode, contract_registry, operation
):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("Requires real PostgreSQL row-lock concurrency")
    row = contract_review(client, episode)
    saved = decide(client, row).json() if operation == "RESUME" else None
    factory = client.app.state.session_factory

    def invoke(_):
        with factory.begin() as db:
            if operation == "DECISION":
                return submit_decision(
                    db,
                    UUID(row["id"]),
                    "service-owner",
                    HumanDecision.model_validate(decision_payload(row)),
                ).id
            return resume_review(
                db,
                UUID(row["id"]),
                "service-owner",
                ReviewResume(decision_id=UUID(saved["id"]), request_sha256=row["request_sha256"]),
            ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(invoke, range(2)))
    assert first == second
    assert count(client, HumanReviewDecision) == 1
    assert count(client, HumanReviewResume) == (1 if operation == "RESUME" else 0)
    if operation == "RESUME":
        with factory() as db:
            assert (
                db.get(Project, UUID(episode["project_id"])).output_profile["applied_reviews"] == 1
            )


def test_snapshot_cannot_change_original_thread_or_duplicate_sources():
    run_id = uuid4()
    value = {
        "project_id": uuid4(),
        "episode_id": uuid4(),
        "run_id": run_id,
        "thread_id": str(uuid4()),
        "kind": "SCRIPT",
        "object_id": uuid4(),
        "version_id": uuid4(),
        "content_sha256": "a" * 64,
        "input_sha256": "b" * 64,
    }
    with pytest.raises(ValidationError):
        ReviewSnapshot.model_validate(value)
    source = uuid4()
    with pytest.raises(ValidationError):
        ReviewSnapshot.model_validate(
            value | {"thread_id": str(run_id), "source_version_ids": [source, source]}
        )


def test_postgres_conflicting_decisions_keep_one_immutable_winner(
    client, episode, contract_registry
):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("Requires real PostgreSQL row-lock concurrency")
    row = contract_review(client, episode)
    factory = client.app.state.session_factory

    def invoke(value):
        try:
            with factory.begin() as db:
                saved = submit_decision(
                    db,
                    UUID(row["id"]),
                    "service-owner",
                    HumanDecision.model_validate(decision_payload(row, value)),
                )
                return "SAVED", saved.decision_json["decision"]
        except DomainError as exc:
            return exc.code, value

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ["APPROVE", "REJECT"]))
    assert sorted(value[0] for value in results) == ["REVIEW_DECISION_CONFLICT", "SAVED"]
    assert count(client, HumanReviewDecision) == 1
    current = client.get(f"/api/v1/human-reviews/{row['id']}").json()
    assert current["decision"]["decision_json"]["decision"] == next(
        value[1] for value in results if value[0] == "SAVED"
    )
