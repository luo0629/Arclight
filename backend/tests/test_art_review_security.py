"""Current roles, exact targets, inputs, locks and superseded approvals."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_art_proposals import proposal_payload
from test_art_review_workflow import decide, decision, effects, existing_payload, path, save, submit
from test_asset_locks import lock, selected
from test_asset_versions import private_objects  # noqa: F401
from test_collaboration import create_episode, create_project, register

from arclight.application.art_proposal_store import save_proposal
from arclight.application.art_proposals import asset_baseline
from arclight.core.errors import DomainError
from arclight.domain.art_review import ArtSave
from arclight.persistence.models import (
    AssetSelectionLock,
    Project,
    ProjectMember,
    User,
    VisualAsset,
)


@pytest.mark.parametrize("role", ["owner", "editor", "viewer", "outsider"])
def test_crud_submit_and_approve_use_actual_current_roles(client, role):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    saved, body = save(client, episode)
    waiting = submit(client, saved).json()
    if role != "owner":
        actor = register(client, role + "@art-review.test")["user"]["id"]
        if role != "outsider":
            with client.app.state.session_factory.begin() as db:
                db.add(
                    ProjectMember(project_id=UUID(project["id"]), user_id=UUID(actor), role=role)
                )
    read = client.get(path(project["id"]) + f"/{saved['id']}")
    assert read.status_code == (404 if role == "outsider" else 200)
    body["creation_key"] = str(uuid4())
    created = client.post(path(project["id"]), json=body)
    assert created.status_code == (
        201 if role in {"owner", "editor"} else 403 if role == "viewer" else 404
    )
    result = decide(client, waiting)
    assert result.status_code == (200 if role == "owner" else 403 if role != "outsider" else 404)
    if role == "owner":
        assert result.json()["review"]["decision"]["actor_id"] == owner["user"]["id"]


@pytest.mark.parametrize("change", ["revoke", "downgrade", "disabled"])
def test_cached_editor_cannot_save_after_authority_changes(client, change):
    register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    actor = register(client, "editor@art-review.test")["user"]["id"]
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.add(ProjectMember(project_id=UUID(project["id"]), user_id=UUID(actor), role="editor"))
    saved, body = save(client, episode)
    with factory() as stale:
        member = stale.scalar(select(ProjectMember))
        stale.commit()
        with factory.begin() as db:
            current = db.scalar(select(ProjectMember))
            if change == "revoke":
                db.delete(current)
            elif change == "downgrade":
                current.role = "viewer"
            else:
                db.get(User, UUID(actor)).disabled = True
        assert member.role == "editor"
        body["creation_key"] = str(uuid4())
        response = client.post(path(project["id"]), json=body)
        assert response.status_code in {401, 403, 404}
        if change != "disabled":
            with pytest.raises(DomainError):
                save_proposal(stale, UUID(project["id"]), actor, ArtSave.model_validate(body))
    assert saved["version"]["version_no"] == 1


def test_saved_approval_does_not_preserve_revoked_original_owner_authority(client):
    from arclight.domain.versions import digest

    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    saved, _ = save(client, episode)
    waiting = submit(client, saved).json()
    review = waiting["review"]
    selected = decision(waiting)["confirmation"]
    accepted = client.post(
        f"/api/v1/human-reviews/{review['id']}/decisions",
        json={
            "version_id": saved["version"]["id"],
            "request_sha256": review["request_sha256"],
            "decision": "APPROVE",
            "impact_digest": digest(selected),
        },
    ).json()
    next_owner = register(client, "next-owner@art-review.test")
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(project["id"])).owner_id = next_owner["user"]["id"]
        db.add(
            ProjectMember(
                project_id=UUID(project["id"]), user_id=UUID(owner["user"]["id"]), role="editor"
            )
        )
    before = effects(client)
    response = client.post(
        f"/api/v1/human-reviews/{review['id']}/resume",
        json={"decision_id": accepted["id"], "request_sha256": review["request_sha256"]},
    )
    assert response.status_code == 403 and effects(client) == before


def test_editor_authors_and_submits_manual_proposal_then_owner_approves(client):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    editor = register(client, "actual-editor@art-review.test")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(project["id"]), user_id=UUID(editor["user"]["id"]), role="editor"
            )
        )
    saved, _ = save(client, episode)
    waiting = submit(client, saved)
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["review"]["requested_by"] == editor["user"]["id"]
    assert decide(client, waiting.json()).status_code == 403
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    approved = decide(client, waiting.json())
    assert approved.status_code == 200, approved.text
    assert approved.json()["outputs"][0]["created_by"] == owner["user"]["id"]


def test_selection_lock_approval_keeps_original_image_lock_and_history(client, episode):
    asset, image = selected(client, episode["project_id"])
    locked = lock(client, asset, image).json()
    payload = proposal_payload(locked["asset_type"], episode["project_id"])
    design = payload["options"][0]["designs"][0]
    with client.app.state.session_factory() as db:
        design["target"] = asset_baseline(db.get(VisualAsset, UUID(locked["id"]))).model_dump(
            mode="json"
        )
        original_lock = db.scalar(select(AssetSelectionLock))
        lock_values = (original_lock.id, original_lock.version_id, original_lock.created_at)
    design.pop("profile", None)
    saved, _ = save(client, episode, payload=payload)
    assert all(
        change["protection"] == "LOCKED"
        for change in saved["version"]["comparison_json"]["options"][0]["changes"]
    )
    waiting = submit(client, saved).json()
    approved = decide(client, waiting)
    assert approved.status_code == 200, approved.text
    current = client.get(f"/api/v1/projects/{episode['project_id']}/assets/{locked['id']}").json()
    assert current["status"] == "LOCKED" and current["lock_version"] == locked["lock_version"]
    assert (
        current["current_version_id"] == image["id"]
        and current["revision"] == locked["revision"] + 1
    )
    with client.app.state.session_factory() as db:
        preserved = db.scalar(select(AssetSelectionLock))
        assert (preserved.id, preserved.version_id, preserved.created_at) == lock_values
    assert (
        client.get(
            f"/api/v1/projects/{episode['project_id']}/assets/{asset['id']}/versions/{image['id']}"
        ).json()["asset_snapshot"]["name"]
        == asset["name"]
    )


@pytest.mark.parametrize("change", ["target", "bible_lock", "source", "project", "episode"])
def test_submission_revalidates_frozen_baselines_and_exact_inputs(client, episode, change):
    from test_art_proposal_references import bound_payload
    from test_art_protection import bible_payload
    from test_visual_bible import edit
    from test_visual_bible import lock as bible_lock

    if change == "bible_lock":
        project = {"id": episode["project_id"]}
        edit(client, project, palette="黑白")
        payload = bible_payload(client, episode, palette="蓝灰")
    elif change == "source":
        payload, _, image = bound_payload(client, episode)
        payload["origin"] = "MANUAL"
    else:
        payload, asset = existing_payload(client, episode)
        payload["episode_id"] = episode["id"]
    saved, _ = save(client, episode, payload=payload)
    with client.app.state.session_factory.begin() as db:
        if change == "target":
            db.get(VisualAsset, UUID(asset["id"])).revision += 1
        elif change == "project":
            db.get(Project, UUID(episode["project_id"])).archived = True
        elif change == "episode":
            from arclight.persistence.models import Episode

            db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
    if change == "bible_lock":
        bible_lock(client, project, 1, ["palette"])
    if change == "source":
        from arclight.persistence.models import AssetVersion

        with client.app.state.session_factory.begin() as db:
            db.get(AssetVersion, UUID(image["asset_version_id"])).stale = True
    before = effects(client)
    assert submit(client, saved).status_code in {409, 422}
    assert effects(client) == before


def test_cross_project_wrong_version_agent_origin_and_reused_keys_are_rejected(client, episode):
    saved, body = save(client, episode)
    other = create_project(client)
    assert client.get(path(other["id"]) + f"/{saved['id']}").status_code == 404
    assert client.post(path(other["id"]), json=body).status_code == 422
    changed = deepcopy(body)
    changed["proposal"]["visual_goal"] = "不同目标"
    assert client.post(path(episode["project_id"]), json=changed).status_code == 409
    changed["proposal"]["origin"] = "AGENT"
    assert client.post(path(episode["project_id"]), json=changed).status_code == 422
    assert (
        client.get(path(episode["project_id"]) + f"/{saved['id']}/versions/{uuid4()}").status_code
        == 404
    )
