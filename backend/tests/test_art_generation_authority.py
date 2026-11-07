"""Publication rechecks the original actor, scope, evidence and current baseline."""

from uuid import UUID, uuid4

import pytest
from art_direction_support import install_direction_model, prepare_direction, reference_set
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_art_direction import facts
from test_art_review_workflow import decide, path
from test_collaboration import create_episode, create_project, register
from test_director_workflow import script

from arclight.application.art_direction import propose_art
from arclight.application.art_generation import generate_art_proposal, publish_art_direction
from arclight.core.errors import DomainError
from arclight.persistence.models import (
    AgentToolGrant,
    AssetVersion,
    Episode,
    Project,
    ProjectMember,
    User,
    VisualAsset,
    WorkflowRun,
)


@pytest.mark.parametrize(
    "change", ["disabled", "project", "episode", "cancelled", "grant", "stale", "scope", "baseline"]
)
def test_first_publication_rechecks_authority_and_inputs_after_completed_model_work(
    client, settings, model_service, change
):
    owner = register(client)
    episode = create_episode(client, create_project(client))
    asset, versions = reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings, target_ids=[asset["id"]])
    sent, errors, _ = install_direction_model(model_service, existing=change == "baseline")
    factory = client.app.state.session_factory
    completed = propose_art(factory, settings, run_id, actor, task)
    assert completed.status == "COMPLETED" and not errors
    with factory.begin() as db:
        if change == "disabled":
            db.get(User, UUID(owner["user"]["id"])).disabled = True
        elif change == "project":
            db.get(Project, UUID(episode["project_id"])).archived = True
        elif change == "episode":
            db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
        elif change == "cancelled":
            db.get(WorkflowRun, run_id).status = "CANCELLED"
        elif change == "grant":
            grant = db.scalar(
                select(AgentToolGrant).where(
                    AgentToolGrant.run_id == run_id, AgentToolGrant.agent_name == "art_director"
                )
            )
            grant.allowed_tools_json = []
        elif change == "stale":
            db.get(AssetVersion, UUID(versions[0]["id"])).stale = True
        elif change == "scope":
            db.get(VisualAsset, UUID(asset["id"])).metadata_json = {
                "usage_scope": {"kind": "EPISODES", "episode_ids": [str(uuid4())]}
            }
        else:
            db.get(VisualAsset, UUID(asset["id"])).revision += 1
    before = facts(client)
    with pytest.raises(DomainError) as rejected:
        publish_art_direction(factory, settings, completed.loop_id, actor)
    assert rejected.value.code in {
        "FORBIDDEN",
        "PROJECT_ARCHIVED",
        "EPISODE_ARCHIVED",
        "AGENT_CANCELLED",
        "USER_NOT_AUTHORIZED",
        "ART_EVIDENCE_REQUIRED",
        "ART_PROPOSAL_BASE_STALE",
    }
    assert facts(client) == before and len(sent) == 4


@pytest.mark.parametrize("delivered", [False, True])
@pytest.mark.parametrize("change", ["revoke", "viewer", "disabled"])
def test_original_editor_authority_is_required_for_first_delivery_and_receipt_replay(
    client, settings, model_service, delivered, change
):
    register(client)
    episode = create_episode(client, create_project(client))
    reference_set(client, episode)
    source = script(client, f"/api/v1/episodes/{episode['id']}")
    editor = register(client, "editor@generated-art.test")
    actor = editor["user"]["id"]
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(episode["project_id"]), user_id=UUID(actor), role="editor"
            )
        )
    run_id, actor, _, task = prepare_direction(client, episode, settings, source=source)
    sent, errors, _ = install_direction_model(model_service)
    completed = propose_art(factory, settings, run_id, actor, task)
    assert completed.status == "COMPLETED" and not errors
    if delivered:
        publish_art_direction(factory, settings, completed.loop_id, actor)
    with factory.begin() as db:
        member = db.scalar(select(ProjectMember).where(ProjectMember.user_id == UUID(actor)))
        if change == "revoke":
            db.delete(member)
        elif change == "viewer":
            member.role = "viewer"
        else:
            db.get(User, UUID(actor)).disabled = True
    before = facts(client)
    with pytest.raises(DomainError):
        publish_art_direction(factory, settings, completed.loop_id, actor)
    with pytest.raises(DomainError):
        generate_art_proposal(factory, settings, run_id, actor, task)
    assert facts(client) == before and len(sent) == 4


def test_editor_generated_proposal_requires_owner_approval_and_keeps_manual_boundary(
    client, settings, model_service
):
    owner = register(client)
    episode = create_episode(client, create_project(client))
    reference_set(client, episode)
    source = script(client, f"/api/v1/episodes/{episode['id']}")
    editor = register(client, "approved-editor@generated-art.test")
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(episode["project_id"]),
                user_id=UUID(editor["user"]["id"]),
                role="editor",
            )
        )
    run_id, actor, _, task = prepare_direction(client, episode, settings, source=source)
    sent, errors, _ = install_direction_model(model_service)
    generated = generate_art_proposal(factory, settings, run_id, actor, task)
    assert not errors and generated.delivery is not None
    delivery = generated.delivery
    endpoint = path(episode["project_id"]) + f"/{delivery.proposal_id}"
    waiting = client.get(endpoint).json()
    assert waiting["review"]["requested_by"] == actor
    assert decide(client, waiting).status_code == 403
    forged = client.post(
        path(episode["project_id"]),
        json={"creation_key": str(uuid4()), "proposal": waiting["version"]["proposal_json"]},
    )
    assert forged.status_code == 422
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    with pytest.raises(DomainError):
        publish_art_direction(factory, settings, delivery.loop_id, owner["user"]["id"])
    approved = decide(client, waiting)
    assert approved.status_code == 200, approved.text
    assert approved.json()["outputs"][0]["created_by"] == owner["user"]["id"]
    other = create_project(client)
    assert client.get(path(other["id"]) + f"/{delivery.proposal_id}").status_code == 404
    changed_task = task.model_copy(update={"visual_goal": "changed goal"})
    before = facts(client)
    with pytest.raises(DomainError, match="冻结目标"):
        generate_art_proposal(factory, settings, run_id, actor, changed_task)
    assert facts(client) == before and len(sent) == 4
