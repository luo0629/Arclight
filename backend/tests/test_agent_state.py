from uuid import uuid4

import pytest
from pydantic import ValidationError

from arclight.domain.agent_state import (
    AgentRunScope,
    AgentRunState,
    AgentStateError,
    initial_agent_state,
    restore_agent_state,
    retry_agent_state,
    terminate_agent_state,
    transition_agent_step,
)


def scope(*, project_id=None, input_sha256=None):
    return AgentRunScope(
        run_id=uuid4(),
        project_id=project_id or uuid4(),
        workflow_type="script_storyboard",
        workflow_version="1.0",
        input_sha256=input_sha256 or "a" * 64,
    )


def test_versioned_plan_transitions_store_only_references_and_check_revision():
    identity = scope()
    draft_id = uuid4()
    observation_id = uuid4()
    state = initial_agent_state(identity)

    waiting = transition_agent_step(
        state,
        identity,
        expected_revision=0,
        step_id="script",
        target="SUCCEEDED",
        next_step_id="script_review",
        next_step_status="WAITING_REVIEW",
        draft_id=draft_id,
        observation_ids=(observation_id,),
    )
    assert waiting.schema_version == 1
    assert waiting.revision == 1 and waiting.status == "WAITING_REVIEW"
    assert waiting.working_drafts[0].artifact_id == draft_id
    assert waiting.observation_refs == [observation_id]
    assert "story" not in waiting.model_dump(mode="json")
    assert (
        transition_agent_step(
            waiting,
            identity,
            expected_revision=1,
            step_id="script",
            target="SUCCEEDED",
            next_step_id="script_review",
            next_step_status="WAITING_REVIEW",
            draft_id=draft_id,
            observation_ids=(observation_id,),
        )
        == waiting
    )

    with pytest.raises(AgentStateError, match="Stale"):
        transition_agent_step(
            waiting,
            identity,
            expected_revision=0,
            step_id="script_review",
            target="SUCCEEDED",
            next_step_id="storyboard",
            next_step_status="RUNNING",
        )
    with pytest.raises(AgentStateError, match="Illegal"):
        transition_agent_step(
            state,
            identity,
            expected_revision=0,
            step_id="script_review",
            target="REJECTED",
            termination_reason="REVIEW_REJECTED",
        )


def test_checkpoint_scope_and_frozen_input_are_verified():
    identity = scope()
    state = initial_agent_state(identity)

    with pytest.raises(AgentStateError, match="scope or frozen input"):
        restore_agent_state(
            state.model_dump(mode="json"),
            None,
            scope(project_id=uuid4(), input_sha256=identity.input_sha256),
        )
    with pytest.raises(AgentStateError, match="scope or frozen input"):
        restore_agent_state(
            state.model_dump(mode="json"),
            None,
            AgentRunScope(
                run_id=identity.run_id,
                project_id=identity.project_id,
                workflow_type=identity.workflow_type,
                workflow_version=identity.workflow_version,
                input_sha256="b" * 64,
            ),
        )


def test_newer_checkpoint_recovers_after_run_transaction_rollback_and_conflicts_fail_closed():
    identity = scope()
    stored = initial_agent_state(identity)
    newer = transition_agent_step(
        stored,
        identity,
        expected_revision=0,
        step_id="script",
        target="SUCCEEDED",
        next_step_id="script_review",
        next_step_status="WAITING_REVIEW",
        draft_id=uuid4(),
    )
    restored = restore_agent_state(
        stored.model_dump(mode="json"),
        {"agent_state": newer.model_dump(mode="json")},
        identity,
    )
    assert restored == newer

    conflicting = transition_agent_step(
        stored,
        identity,
        expected_revision=0,
        step_id="script",
        target="FAILED",
        termination_reason="EXECUTION_FAILED",
    )
    with pytest.raises(AgentStateError, match="conflict"):
        restore_agent_state(
            conflicting.model_dump(mode="json"),
            {"agent_state": newer.model_dump(mode="json")},
            identity,
        )

    unsupported = {**stored.model_dump(mode="json"), "schema_version": 2}
    with pytest.raises(AgentStateError, match="unsupported version"):
        restore_agent_state(unsupported, None, identity)


def test_legacy_text_checkpoint_is_lazily_migrated_without_copying_input_text():
    identity = scope()
    draft_id = uuid4()
    observation_id = uuid4()
    migrated = restore_agent_state(
        None,
        {
            "run_id": str(identity.run_id),
            "story": "private historical input",
            "script": {"scenes": []},
            "script_version_id": str(draft_id),
            "context_refs": {
                "script": {
                    "startup_manifest_id": str(observation_id),
                    "evidence_manifest_ids": [],
                }
            },
        },
        identity,
    )
    assert migrated.revision == 1
    assert migrated.status == "WAITING_REVIEW"
    assert migrated.steps[0].status == "SUCCEEDED"
    assert migrated.steps[1].status == "WAITING_REVIEW"
    assert migrated.working_drafts[0].artifact_id == draft_id
    assert migrated.observation_refs == [observation_id]
    assert "private historical input" not in str(migrated.model_dump(mode="json"))
    with pytest.raises(AgentStateError, match="different run"):
        restore_agent_state(None, {"run_id": str(uuid4())}, identity)


def test_terminal_reasons_cancel_and_explicit_retry_transitions():
    identity = scope()
    failed = transition_agent_step(
        initial_agent_state(identity),
        identity,
        expected_revision=0,
        step_id="script",
        target="FAILED",
        termination_reason="EXECUTION_FAILED",
    )
    retried = retry_agent_state(failed)
    assert retried.status == "RUNNING" and retried.termination_reason is None
    assert retried.revision == failed.revision + 1
    assert all(step.status == "PENDING" for step in retried.steps)

    cancelled = terminate_agent_state(initial_agent_state(identity), identity, reason="CANCELLED")
    assert cancelled.status == "CANCELLED"
    assert cancelled.termination_reason == "CANCELLED"
    assert all(step.status == "SKIPPED" for step in cancelled.steps)
    with pytest.raises(AgentStateError, match="Only an execution-failed"):
        retry_agent_state(cancelled)


def test_extensions_are_namespaced_and_sensitive_fields_are_rejected():
    identity = scope()
    payload = initial_agent_state(identity).model_dump(mode="json")
    payload["extensions"] = {"screenwriter.planning": {"attempt": 1}}
    AgentRunState.model_validate(payload)

    payload["extensions"] = {"planning": {"attempt": 1}}
    with pytest.raises(ValidationError, match="namespace"):
        AgentRunState.model_validate(payload)
    payload["extensions"] = {"screenwriter.planning": {"api_key": "private"}}
    with pytest.raises(ValidationError):
        AgentRunState.model_validate(payload)


def test_legacy_checkpoint_before_first_node_keeps_initial_revision():
    identity = scope()
    initial = initial_agent_state(identity)
    restored = restore_agent_state(
        initial, {"run_id": str(identity.run_id), "story": "frozen input"}, identity
    )
    assert restored == initial


@pytest.mark.parametrize(
    "status,reason",
    [("FAILED", "COMPLETED"), ("SUCCEEDED", "CANCELLED"), ("RUNNING", "EXECUTION_FAILED")],
)
def test_status_cannot_claim_another_termination_reason(status, reason):
    payload = initial_agent_state(scope()).model_dump(mode="json")
    payload.update(status=status, termination_reason=reason)
    with pytest.raises(ValidationError, match="termination reason"):
        AgentRunState.model_validate(payload)


def test_extensions_are_bounded_and_drafts_cannot_escape_the_plan():
    payload = initial_agent_state(scope()).model_dump(mode="json")
    payload["extensions"] = {"screenwriter.planning": {"text": "x" * 16384}}
    with pytest.raises(ValidationError, match="size limit"):
        AgentRunState.model_validate(payload)
    payload["extensions"] = {}
    payload["working_drafts"] = [{"step_id": "outline", "artifact_id": str(uuid4())}]
    with pytest.raises(ValidationError, match="belong to the Agent plan"):
        AgentRunState.model_validate(payload)


@pytest.mark.parametrize("dialect", ["sqlite:///unused.db", "postgresql+psycopg://unused/unused"])
def test_agent_state_offline_upgrade_and_preservation_check(dialect):
    from io import StringIO
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = dialect
    command.upgrade(config, "0042:0043", sql=True)
    assert "ALTER TABLE workflow_runs ADD COLUMN agent_state_json" in output.getvalue()
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0043:0042", sql=True)
