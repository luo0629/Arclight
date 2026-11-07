"""AART-03 actual workflow, original generation/import, image comparison and ART-04."""

from uuid import UUID

from art_concept_support import complete_concept_job, install_concept_model, start_concepts
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_art_review_workflow import decide
from test_art_workflow import drive, progress, proposal

from arclight.application.agent_external_waits import queue_ready_external
from arclight.application.art_concept_jobs import concept_jobs, concept_origin
from arclight.main import create_app
from arclight.persistence.art_proposals import ArtApprovedVersion
from arclight.persistence.models import AssetVersion, VisualAsset, WorkflowRun


def test_two_concept_rounds_wait_restart_compare_revise_and_approve_exact_references(
    client, episode, settings, model_service
):
    sent, errors, drafts = install_concept_model(model_service)
    run_id, body = start_concepts(client, episode, settings)
    waiting = drive(client, settings, run_id)
    assert waiting["status"] == "RUNNING", waiting
    first_progress = progress(client, run_id)
    assert first_progress["operations"][0]["status"] == "WAITING_EXTERNAL", (first_progress, errors)
    loop_id = first_progress["operations"][0]["loop_id"]
    assert first_progress["can_cancel"] and not first_progress["delivery"]
    assert len(sent) == 3 and not errors
    assert queue_ready_external(client.app.state.session_factory, settings) == 0
    assert len(sent) == 3
    for round_no in (1, 2):
        current = progress(client, run_id)["concept_rounds"][-1]
        assert current["round_no"] == round_no
        assert complete_concept_job(client, current["job_id"]) == 2
        assert queue_ready_external(client.app.state.session_factory, settings) == 1
        assert queue_ready_external(client.app.state.session_factory, settings) == 0
        with TestClient(create_app(settings)) as restarted:
            restarted.cookies.update(client.cookies)
            restarted.headers.update(client.headers)
            state = drive(restarted, settings, run_id)
            assert state["status"] == ("RUNNING" if round_no == 1 else "WAITING_REVIEW"), (
                state.get("error_code"),
                progress(restarted, run_id)["operations"][0]["stop_code"],
                errors,
            )
        assert not errors, errors
    detail = progress(client, run_id)
    assert detail["operations"][0]["loop_id"] == loop_id
    assert detail["operations"][0]["status"] == "COMPLETED"
    assert len(detail["concept_rounds"]) == 2 and len(drafts) == 2
    assert len(drafts[-1]["proposal"]["references"]) == 4
    assert any(
        isinstance(message.get("content"), list)
        and any(part.get("type") == "image_url" for part in message["content"])
        for payload in sent
        for message in payload["messages"]
    )
    with client.app.state.session_factory() as db:
        jobs = concept_jobs(db, run_id=UUID(run_id))
        assert len(jobs) == 2 and all(job.attempt_count == 1 for job in jobs)
        assert concept_origin(db, jobs[1])["previous_comparison_invocation_id"]
        assert db.get(WorkflowRun, UUID(run_id)).workflow_version == "art-proposal/2.0"
        versions = [
            db.get(AssetVersion, UUID(ref["asset"]["asset_version_id"]))
            for ref in drafts[-1]["proposal"]["references"]
        ]
        assert all(row.status == "DRAFT" for row in versions)
        assert all(db.get(VisualAsset, row.asset_id).current_version_id is None for row in versions)
    saved = proposal(client, episode, detail["delivery"])
    changes = saved["version"]["comparison_json"]["options"][0]["changes"]
    assert sum(row["field_path"] == "reference.approval_and_rights" for row in changes) == 4
    approved = decide(client, saved)
    assert approved.status_code == 200, approved.text
    with client.app.state.session_factory() as db:
        refs = [
            UUID(ref["asset"]["asset_version_id"]) for ref in drafts[-1]["proposal"]["references"]
        ]
        assert all(db.get(AssetVersion, key).status == "APPROVED" for key in refs)
        records = list(db.scalars(select(ArtApprovedVersion)))
        assert sum(row.content_json.get("kind") == "CONCEPT_REFERENCE" for row in records) == 4
        assert all(
            db.get(VisualAsset, db.get(AssetVersion, key).asset_id).current_version_id is None
            for key in refs
        )
