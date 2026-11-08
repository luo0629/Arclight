from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import select, text
from test_agent_model import model_service as model_service
from test_agent_runtime import tool_feedback
from test_concurrency import require_postgres
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviewer import run_review
from test_continuity_rules import rules_storage as rules_storage

from arclight.application.continuity_reviewer import resume_reviewer
from arclight.core.errors import DomainError
from arclight.persistence.models import AgentToolGrant, AssetVersion, WorkflowRun


def test_valid_but_unobserved_reference_cannot_be_used_in_the_final_review(
    client,
    settings,
    model_service,
    reviewer_case,
):
    def add_unobserved(result):
        reference = reviewer_case["refs"]["SECOND_CHARACTER"]
        with client.app.state.session_factory() as db:
            version = db.get(AssetVersion, UUID(reference["asset_version_id"]))
            result["references"].append(
                {
                    "reference_id": str(uuid4()),
                    "role": "REFERENCE",
                    "resource_id": str(version.id),
                    "content_sha256": version.checksum,
                    "asset_id": str(version.asset_id),
                    "asset_type": "CHARACTER",
                }
            )

    _, errors, _ = install_reviewer_model(model_service, dynamic=False, mutate_final=add_unobserved)
    result = run_review(client, settings, reviewer_case)
    assert not errors, errors
    assert result.status == "STOPPED" and result.stop_code == "REVIEW_REFERENCE_UNOBSERVED", (
        result.model_dump_json()
    )


def test_other_invocation_evidence_cannot_replace_this_loops_investigation(
    client,
    settings,
    model_service,
    reviewer_case,
):
    _, errors, _ = install_reviewer_model(
        model_service, dynamic=False, steal_manifest=reviewer_case["evidence"]["manifest_id"]
    )
    result = run_review(client, settings, reviewer_case)
    assert not errors, errors
    assert result.status == "STOPPED" and result.stop_code == "REVIEW_EVIDENCE_REQUIRED", (
        result.model_dump_json()
    )


def test_revoked_tool_grant_blocks_resume_before_more_model_calls(
    client,
    settings,
    model_service,
    reviewer_case,
):
    sent, errors, _ = install_reviewer_model(model_service, human=True)
    waiting = run_review(client, settings, reviewer_case)
    assert waiting.status == "HUMAN_REVIEW", waiting.model_dump_json()
    factory = client.app.state.session_factory
    with factory.begin() as db:
        grant = db.scalar(
            select(AgentToolGrant).where(
                AgentToolGrant.run_id == waiting.run_id,
                AgentToolGrant.agent_name == "continuity_reviewer",
            )
        )
        grant.allowed_tools_json = []
    count = len(sent)
    with pytest.raises(DomainError) as denied:
        resume_reviewer(factory, settings, waiting.loop_id, reviewer_case["actor"])
    assert denied.value.code == "USER_NOT_AUTHORIZED"
    assert len(sent) == count and not errors


def test_postgres_duplicate_reviewer_loops_release_business_locks_during_model_io(
    client,
    settings,
    model_service,
    reviewer_case,
):
    require_postgres(settings)
    factory = client.app.state.session_factory
    entered, release = Event(), Event()

    def pause(payload):
        if not tool_feedback(payload):
            entered.set()
            assert release.wait(25)

    sent, errors, _ = install_reviewer_model(model_service, dynamic=False, on_http=pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(run_review, client, settings, reviewer_case)
        try:
            assert entered.wait(15)
            with factory.begin() as db:
                db.execute(text("SET LOCAL lock_timeout='2s'"))
                assert (
                    db.scalar(
                        select(WorkflowRun)
                        .where(WorkflowRun.id == reviewer_case["run_id"])
                        .with_for_update()
                    ).status
                    == "RUNNING"
                )
            duplicate = pool.submit(run_review, client, settings, reviewer_case)
            with pytest.raises(DomainError) as busy:
                duplicate.result(timeout=10)
            assert busy.value.code == "AGENT_LOOP_BUSY"
        finally:
            release.set()
        result = pending.result(timeout=30)
    assert not errors, errors
    assert result.status == "COMPLETED" and len(sent) == 5, result.model_dump_json()
