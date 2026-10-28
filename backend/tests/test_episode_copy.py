"""STORY-03: independent drafts, exact reusable assets, no production state."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_api_workflow import start
from test_collaboration import create_episode, create_project, register
from test_text_contract_v2 import legacy_script, make_asset, save_script

from arclight.agents.contracts import ScriptContent
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    CanvasLayout,
    Episode,
    OutboxEvent,
    Review,
    Script,
    Storyboard,
    VisualAsset,
    WorkflowEvent,
    WorkflowRun,
)
from arclight.providers.text import MockTextProvider


def path(episode):
    return f"/api/v1/episodes/{episode['id']}"


def duplicate(client, episode, **values):
    return client.post(
        path(episode) + "/copy", json={"episode_no": 2, "title": "新集草稿", **values}
    )


def source_content(client, episode, with_assets=False):
    content = {"estimated_duration_ms": 30000, **legacy_script()}
    references = {}
    if with_assets:
        with client.app.state.session_factory.begin() as db:
            references = {
                kind: make_asset(db, episode["project_id"], kind)
                for kind in ("CHARACTER", "SCENE", "LOCATION", "PROP", "COSTUME", "IMAGE")
            }
        content["character_assets"] = [{"character": "主角", "asset": references["CHARACTER"]}]
        content["scenes"][0].update(
            scene_asset=references["SCENE"],
            location_asset=references["LOCATION"],
            props=[references["PROP"]],
        )
    script = save_script(client, episode, content)
    text = ScriptContent.model_validate(script["content_json"])
    board = MockTextProvider().storyboard(text, 30000).model_dump(mode="json")
    for shot in board["shots"]:
        shot.update(
            scene_id=text.scenes[0].scene_id, dialogue_line_ids=[text.scenes[0].dialogue[0].line_id]
        )
        if references:
            shot.update(
                characters=["主角"],
                character_details=[
                    {
                        "character": "主角",
                        "asset": references["CHARACTER"],
                        "costume": references["COSTUME"],
                        "action": "驻足",
                    }
                ],
                scene_asset=references["SCENE"],
                location_asset=references["LOCATION"],
                props=[references["PROP"]],
                selected_visual_asset=references["IMAGE"],
            )
    response = client.post(
        path(episode) + "/storyboards",
        json={
            "expected_version_no": 0,
            "script_version_id": script["id"],
            "content": board,
        },
    )
    assert response.status_code == 201, response.text
    saved_board = response.json()
    approved = client.post(
        path(episode) + f"/storyboards/{saved_board['id']}/review", json={"decision": "APPROVE"}
    )
    assert approved.status_code == 200, approved.text
    return script, saved_board, references


def versions(client, episode, kind):
    return client.get(path(episode) + "/" + kind).json()


def test_independent_copy_rebinds_versions_ids_and_requires_fresh_reviews(client, episode):
    script, board, _ = source_content(client, episode)
    original_scripts = versions(client, episode, "scripts")
    original_boards = versions(client, episode, "storyboards")
    original_shots = client.get(
        path(episode) + "/shots", params={"storyboard_id": board["id"]}
    ).json()
    assert (
        client.put(
            path(episode) + "/canvas/layout",
            json={
                "expected_revision": 0,
                "positions": {},
                "viewport": {"x": 8, "y": 9, "zoom": 1},
            },
        ).status_code
        == 200
    )
    copied = duplicate(client, episode, title="  新集草稿  ")
    assert copied.status_code == 201, copied.text
    target = copied.json()
    assert target["id"] != episode["id"] and target["status"] == "DRAFT"
    assert target["project_id"] == episode["project_id"] and target["title"] == "新集草稿"
    assert target["target_duration_ms"] == episode["target_duration_ms"]
    (new_script,) = versions(client, target, "scripts")
    (new_board,) = versions(client, target, "storyboards")
    assert new_script["run_id"] is None and new_board["run_id"] is None
    assert new_script["status"] == new_board["status"] == "READY_FOR_REVIEW"
    assert new_script["version_no"] == new_board["version_no"] == 1
    assert new_board["script_version_id"] == new_script["id"] != script["id"]
    old_scene = ScriptContent.model_validate(script["content_json"]).scenes[0]
    new_scene = new_script["content_json"]["scenes"][0]
    assert new_scene["scene_id"] != old_scene.scene_id
    assert new_scene["dialogue"][0]["line_id"] != old_scene.dialogue[0].line_id
    for shot in new_board["content_json"]["shots"]:
        assert shot["scene_id"] == new_scene["scene_id"]
        assert shot["dialogue_line_ids"] == [new_scene["dialogue"][0]["line_id"]]
    new_shots = client.get(
        path(target) + "/shots", params={"storyboard_id": new_board["id"]}
    ).json()
    assert len(new_shots) == len(original_shots)
    assert not {s["id"] for s in new_shots} & {s["id"] for s in original_shots}
    assert all(s["status"] == "READY_FOR_REVIEW" for s in new_shots)
    review_path = path(target) + f"/storyboards/{new_board['id']}/review"
    assert client.post(review_path, json={"decision": "APPROVE"}).status_code == 409
    assert (
        client.post(
            path(target) + f"/scripts/{new_script['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    assert client.post(review_path, json={"decision": "APPROVE"}).status_code == 200
    assert versions(client, episode, "scripts") == original_scripts
    assert versions(client, episode, "storyboards") == original_boards
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(CanvasLayout).where(CanvasLayout.episode_id == UUID(target["id"])))
            is None
        )
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "episode.copied"))
        assert event.details_json["source_episode_id"] == episode["id"]


@pytest.mark.legacy_schema
@pytest.mark.parametrize("reuse", [True, False])
def test_reuse_exact_assets_but_not_shot_results_or_quality_approval(
    client, episode, reuse, protect_history
):
    script, board, refs = source_content(client, episode, with_assets=True)
    with client.app.state.session_factory.begin() as db:
        # Even an old quality relation must not leak into the new draft.
        old_board = db.get(Storyboard, UUID(board["id"]))
        content = deepcopy(old_board.content_json)
        content["shots"][0]["quality_review"] = {
            "review_id": str(uuid4()),
            "asset_version_id": refs["IMAGE"]["asset_version_id"],
        }
        old_board.content_json = content
        # The copied identity is the pinned version, not the asset's newest pointer.
        asset = db.get(VisualAsset, UUID(refs["CHARACTER"]["asset_id"]))
        asset.current_version_id = UUID(refs["IMAGE"]["asset_version_id"])
    protect_history()
    response = duplicate(client, episode, reuse_assets=reuse)
    assert response.status_code == 201, response.text
    target = response.json()
    (text,) = versions(client, target, "scripts")
    (copied_board,) = versions(client, target, "storyboards")
    scene = text["content_json"]["scenes"][0]
    if reuse:
        assert text["content_json"]["character_assets"][0]["asset"] == refs["CHARACTER"]
        assert scene["scene_asset"] == refs["SCENE"] and scene["props"] == [refs["PROP"]]
    else:
        assert text["content_json"]["character_assets"] == []
        assert scene["scene_asset"] is None and scene["props"] == []
    for shot in copied_board["content_json"]["shots"]:
        assert shot["selected_visual_asset"] is None and shot["quality_review"] is None
        assert shot["character_details"][0]["asset"] == (refs["CHARACTER"] if reuse else None)
        assert shot["character_details"][0]["costume"] == (refs["COSTUME"] if reuse else None)
        assert shot["character_details"][0]["action"] == "驻足"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AssetVersion)) == 6
        assert db.scalar(select(func.count()).select_from(VisualAsset)) == 6


@pytest.mark.legacy_schema
@pytest.mark.parametrize(
    "invalid", ["stale", "unapproved", "archived", "foreign", "wrong_kind", "missing"]
)
def test_unusable_asset_references_are_removed_without_losing_text(
    client, episode, invalid, protect_history
):
    script, board, refs = source_content(client, episode, with_assets=True)
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(refs["CHARACTER"]["asset_version_id"]))
        asset = db.get(VisualAsset, UUID(refs["CHARACTER"]["asset_id"]))
        if invalid == "stale":
            version.stale = True
        elif invalid == "unapproved":
            version.status = "READY_FOR_REVIEW"
        elif invalid == "archived":
            asset.status = "ARCHIVED"
        elif invalid == "wrong_kind":
            asset.asset_type = "PROP"
        elif invalid == "foreign":
            other = client.post("/api/v1/projects", json={"name": "其他项目"}).json()
            asset.project_id = UUID(other["id"])
        else:
            for model, row_id in ((Script, script["id"]), (Storyboard, board["id"])):
                row = db.get(model, UUID(row_id))
                content = deepcopy(row.content_json)
                binding = (
                    content["character_assets"][0]
                    if model is Script
                    else content["shots"][0]["character_details"][0]
                )
                binding["asset"]["asset_version_id"] = str(UUID(int=1))
                if model is Storyboard:
                    for shot in content["shots"]:
                        shot["character_details"][0]["asset"]["asset_version_id"] = str(UUID(int=1))
                row.content_json = content
    protect_history()
    response = duplicate(client, episode)
    assert response.status_code == 201, response.text
    target = response.json()
    (text,) = versions(client, target, "scripts")
    (copied_board,) = versions(client, target, "storyboards")
    assert text["content_json"]["character_assets"] == []
    assert text["content_json"]["scenes"][0]["action"] == "主角驻足"
    assert all(
        s["character_details"][0]["asset"] is None for s in copied_board["content_json"]["shots"]
    )
    assert text["content_json"]["scenes"][0]["scene_asset"] == refs["SCENE"]


def test_empty_episode_and_duplicate_number_are_atomic(client, episode):
    first = duplicate(client, episode)
    assert first.status_code == 201, first.text
    assert versions(client, first.json(), "scripts") == []
    assert versions(client, first.json(), "storyboards") == []
    assert duplicate(client, episode).status_code == 409
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Episode)) == 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "episode.copied")
            )
            == 1
        )


@pytest.mark.parametrize("status", ["REJECTED", "STALE", "ARCHIVED"])
def test_skip_unusable_versions_and_never_mismatch_script_board(client, episode, status):
    script, board, _ = source_content(client, episode)
    with client.app.state.session_factory.begin() as db:
        db.add(
            Script(
                episode_id=UUID(episode["id"]),
                version_no=2,
                content_json=script["content_json"],
                status=status,
            )
        )
        db.get(Storyboard, UUID(board["id"])).status = status
    copied = duplicate(client, episode).json()
    assert len(versions(client, copied, "scripts")) == 1
    assert versions(client, copied, "storyboards") == []
    with client.app.state.session_factory.begin() as db:
        db.get(Script, UUID(script["id"])).status = status
    copied = duplicate(client, episode, episode_no=3).json()
    assert versions(client, copied, "scripts") == []


def test_newest_script_does_not_pick_an_older_scripts_board(client, episode):
    _, _, _ = source_content(client, episode)
    response = client.post(
        path(episode) + "/scripts",
        json={
            "expected_version_no": 1,
            "content": {"estimated_duration_ms": 30000, **legacy_script(), "title": "更新的稿件"},
        },
    )
    assert response.status_code == 201
    target = duplicate(client, episode).json()
    assert versions(client, target, "scripts")[0]["content_json"]["title"] == "更新的稿件"
    assert versions(client, target, "storyboards") == []


@pytest.mark.legacy_schema
def test_legacy_content_and_invalid_sources(client, episode, protect_history):
    with client.app.state.session_factory.begin() as db:
        source = Script(
            episode_id=UUID(episode["id"]),
            version_no=1,
            content_json=legacy_script(),
            status="APPROVED",
        )
        db.add(source)
    target = duplicate(client, episode).json()
    assert versions(client, target, "scripts")[0]["content_json"]["schema_version"] == "2.0"
    with client.app.state.session_factory.begin() as db:
        source = db.scalar(select(Script).where(Script.episode_id == UUID(episode["id"])))
        source.content_json = {"corrupt": True}
    protect_history()
    failed = duplicate(client, episode, episode_no=3)
    assert failed.status_code == 409 and failed.json()["error"]["code"] == "INVALID_COPY_SOURCE"
    assert len(client.get(f"/api/v1/projects/{episode['project_id']}/episodes").json()) == 2


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "WAITING_REVIEW"])
def test_active_workflows_block_copy(client, episode, status):
    run = start(client, episode).json()
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, UUID(run["run_id"])).status = status
    response = duplicate(client, episode)
    assert response.status_code == 409 and response.json()["error"]["code"] == "ACTIVE_WORKFLOW"


@pytest.mark.legacy_schema
@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "SUCCEEDED", "REJECTED"])
def test_terminal_runs_events_outbox_and_reviews_are_not_copied(
    client, episode, status, protect_history
):
    script, _, _ = source_content(client, episode)
    run = start(client, episode).json()
    with client.app.state.session_factory.begin() as db:
        row = db.get(WorkflowRun, UUID(run["run_id"]))
        row.status = status
        db.get(Script, UUID(script["id"])).run_id = row.id
        db.add(
            Review(
                run_id=row.id,
                resource_type="script",
                version_id=UUID(script["id"]),
                reviewer_id="service-owner",
                decision="APPROVE",
            )
        )
    protect_history()
    models = (WorkflowRun, WorkflowEvent, OutboxEvent, Review)
    with client.app.state.session_factory() as db:
        before = [db.scalar(select(func.count()).select_from(model)) for model in models]
    response = duplicate(client, episode)
    assert response.status_code == 201, response.text
    assert versions(client, response.json(), "scripts")[0]["run_id"] is None
    with client.app.state.session_factory() as db:
        assert [db.scalar(select(func.count()).select_from(model)) for model in models] == before
        assert (
            db.scalar(
                select(WorkflowRun).where(WorkflowRun.episode_id == UUID(response.json()["id"]))
            )
            is None
        )


@pytest.mark.parametrize("scope", ["project", "episode"])
def test_archive_gate(client, episode, scope):
    url = f"/api/v1/projects/{episode['project_id']}" if scope == "project" else path(episode)
    assert client.patch(url, json={"archived": True}).status_code == 200
    assert duplicate(client, episode).status_code == 409


@pytest.mark.parametrize(
    "role,code", [("owner", 201), ("editor", 201), ("viewer", 403), ("outsider", 404)]
)
def test_copy_permissions_and_isolation(client, role, code):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    if role != "owner":
        member = register(client, "member@example.com")
        if role != "outsider":
            client.headers["Authorization"] = "Bearer " + owner["access_token"]
            assert (
                client.post(
                    f"/api/v1/projects/{project['id']}/members",
                    json={
                        "email": member["user"]["email"],
                        "role": role,
                    },
                ).status_code
                == 201
            )
        client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert duplicate(client, episode).status_code == code


@pytest.mark.parametrize(
    "values",
    [
        {"episode_no": 0},
        {"episode_no": -1},
        {"episode_no": 1.5},
        {"episode_no": True},
        {"episode_no": 2147483648},
        {"title": " "},
        {"title": "a" * 161},
        {"reuse_assets": "yes"},
        {"reuse_assets": None},
        {"project_id": str(uuid4())},
    ],
)
def test_invalid_copy_payload(client, episode, values):
    assert duplicate(client, episode, **values).status_code == 422
    assert len(client.get(f"/api/v1/projects/{episode['project_id']}/episodes").json()) == 1
