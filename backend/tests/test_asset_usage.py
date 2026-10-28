"""ASSET-09 actual reverse references across immutable scripts and shots."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from test_assets import archive, create, path
from test_collaboration import create_project, register
from test_text_contract_v2 import (
    board_content,
    legacy_script,
    make_asset,
    save_board,
    save_script,
)

from arclight.persistence.models import AssetVersion, ProjectMember


@pytest.fixture
def usage_assets(client, episode):
    with client.app.state.session_factory.begin() as db:
        return {
            kind: make_asset(db, episode["project_id"], kind)
            for kind in ("CHARACTER", "SCENE", "PROP", "COSTUME")
        }


def usage(client, episode, reference, **query):
    return client.get(path(episode["project_id"]) + f"/{reference['asset_id']}/usage", params=query)


def populated(client, episode, usage_assets):
    content = legacy_script()
    content["character_assets"] = [{"character": "主角", "asset": usage_assets["CHARACTER"]}]
    content["scenes"][0].update(scene_asset=usage_assets["SCENE"], props=[usage_assets["PROP"]])
    script = save_script(client, episode, content)
    board = board_content(script)
    board["shots"][0]["character_details"][0]["costume"] = usage_assets["COSTUME"]
    board["shots"][0]["duration_ms"] = 15000
    second = deepcopy(board["shots"][0])
    second["shot_no"] = 2
    board["shots"].append(second)
    response = save_board(client, episode, script, board)
    assert response.status_code == 201, response.text
    return script, response.json()


def test_exact_usage_across_script_scene_and_two_shots(client, episode, usage_assets):
    script, board = populated(client, episode, usage_assets)
    rows = usage(client, episode, usage_assets["SCENE"]).json()
    assert len(rows) == 3
    assert [row["shot_no"] for row in rows] == [None, 1, 2]
    assert [row["content_id"] for row in rows] == [script["id"], board["id"], board["id"]]
    assert rows[0]["scene_title"] == "街道"
    assert len({row["scene_id"] for row in rows}) == 1
    assert all(row["episode_id"] == episode["id"] for row in rows)
    assert all(row["asset_version_id"] == usage_assets["SCENE"]["asset_version_id"] for row in rows)
    assert rows[0]["content_status"] == "APPROVED"
    assert rows[1]["content_status"] == "READY_FOR_REVIEW"
    assert len(usage(client, episode, usage_assets["CHARACTER"]).json()) == 3
    assert len(usage(client, episode, usage_assets["PROP"]).json()) == 3
    # Non-directory legacy types remain outside this directory endpoint.
    assert usage(client, episode, usage_assets["COSTUME"]).status_code == 404


def test_usage_exact_filters_pagination_and_historical_preservation(client, episode, usage_assets):
    populated(client, episode, usage_assets)
    ref = usage_assets["SCENE"]
    original = usage(client, episode, ref).json()
    pages = [usage(client, episode, ref, limit=1, offset=i).json()[0] for i in range(3)]
    assert pages == original
    assert usage(client, episode, ref, limit=1, offset=3).json() == []
    assert usage(client, episode, ref, version_id=ref["asset_version_id"]).json() == original
    assert usage(client, episode, ref, episode_id=episode["id"]).json() == original
    with client.app.state.session_factory.begin() as db:
        new = AssetVersion(
            asset_id=UUID(ref["asset_id"]),
            version_no=2,
            object_key="unused/new",
            checksum="b" * 64,
            created_by="service-owner",
            status="APPROVED",
        )
        db.add(new)
        db.flush()
        new_id = str(new.id)
        db.get(AssetVersion, UUID(ref["asset_version_id"])).stale = True
    row = client.get(path(episode["project_id"]) + "/" + ref["asset_id"]).json()
    archive(client, row)
    assert usage(client, episode, ref).json() == original
    assert usage(client, episode, ref, version_id=new_id).json() == []
    for query in ({"version_id": str(uuid4())}, {"episode_id": str(uuid4())}):
        assert usage(client, episode, ref, **query).status_code == 404


def test_declared_scope_and_uuid_in_notes_are_not_actual_usage(client, episode):
    asset = create(client, episode["project_id"], "SCENE")
    content = legacy_script()
    content["scenes"][0]["notes"] = asset["id"]
    save_script(client, episode, content)
    assert usage(client, episode, {"asset_id": asset["id"]}).json() == []


def test_usage_in_multiple_episodes_and_content_versions(client, episode, usage_assets):
    script, _ = populated(client, episode, usage_assets)
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/scripts",
        json={"expected_version_no": 1, "content": script["content_json"]},
    )
    assert response.status_code == 201, response.text
    other = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "另集", "target_duration_ms": 30000},
    ).json()
    save_script(client, other, script["content_json"])
    rows = usage(client, episode, usage_assets["SCENE"]).json()
    assert len(rows) == 5
    assert {row["episode_no"] for row in rows} == {1, 2}
    assert {row["content_version_no"] for row in rows} == {1, 2}
    assert len(usage(client, episode, usage_assets["SCENE"], episode_id=other["id"]).json()) == 1


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_usage_project_permissions_and_foreign_filters(client, role):
    register(client)
    project = create_project(client)
    asset = create(client, project["id"], "SCENE")
    member = register(client, f"usage-{role}@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    endpoint = path(project["id"]) + f"/{asset['id']}/usage"
    assert client.get(endpoint).status_code == (404 if role == "outsider" else 200)
    foreign = create_project(client)
    assert client.get(path(foreign["id"]) + f"/{asset['id']}/usage").status_code == 404
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, foreign["id"], "SCENE")
    assert (
        client.get(endpoint, params={"version_id": reference["asset_version_id"]}).status_code
        == 404
    )


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
def test_usage_pagination_bounds(client, episode, query):
    row = create(client, episode["project_id"])
    assert usage(client, episode, {"asset_id": row["id"]}, **query).status_code == 422
