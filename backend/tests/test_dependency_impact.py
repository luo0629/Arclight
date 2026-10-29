"""DAG-04: read-only exact impact previews and confirmation race protection."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_asset_selection import approved, choose, current
from test_asset_versions import private_objects, versions  # noqa: F401
from test_assets import create
from test_collaboration import create_project, register
from test_dependency_invalidation import change, produce
from test_dependency_validation import registry_version

from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    DependencyInvalidation,
    VisualAsset,
)
from arclight.providers.text import MockTextProvider


def path(pid):
    return f"/api/v1/projects/{pid}"


def test_preview_is_complete_deterministic_and_never_writes(client, episode):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        script = registry_version(db, pid, kind="SCRIPT")
        board = produce(db, pid, "STORYBOARD", script)
        image = produce(db, pid, "SHOT_IMAGE", board)
        render = produce(db, pid, "RENDER", image, "RENDER_DEPENDENCY")
        source_id = str(script[1].id)
        before = [(v.id, v.revision, v.status, v.stale) for v in db.scalars(select(AssetVersion))]
        audits = db.scalar(select(func.count()).select_from(AuditEvent))
    url = path(pid) + "/dependency-impact?asset_version_id=" + source_id
    first = client.get(url)
    assert first.status_code == 200, first.text
    assert client.get(url).json() == first.json()
    result = first.json()
    assert result["summary"] == {"REVIEW": 1, "GENERATE": 1, "RENDER": 1}
    assert [item["depth"] for item in result["affected"]] == [1, 2, 3]
    assert {x["asset_version_id"] for x in result["affected"]} == {
        str(pair[1].id) for pair in (board, image, render)
    }
    with client.app.state.session_factory() as db:
        assert [
            (v.id, v.revision, v.status, v.stale) for v in db.scalars(select(AssetVersion))
        ] == before
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == audits
        assert db.scalar(select(func.count()).select_from(DependencyInvalidation)) == 0


def test_selector_preview_matches_exact_dialogue_propagation(client, episode):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        script = registry_version(db, pid, kind="SCRIPT")
        audio = produce(db, pid, "AUDIO", script, selectors=["dialogue:one"])
        produce(db, pid, "AUDIO", script, selectors=["dialogue:two"])
        produce(db, pid, "SHOT_IMAGE", script)
        body = {"changes": [change(script[1], "DIALOGUE", ["dialogue:one"])]}
    response = client.post(path(pid) + "/dependencies/impact", json=body)
    assert response.status_code == 200, response.text
    assert [x["asset_version_id"] for x in response.json()["affected"]] == [str(audio[1].id)]


def test_selection_rechecks_preview_before_any_mutation(client, episode):
    pid = UUID(episode["project_id"])
    asset = create(client, str(pid))
    first = approved(client, asset)
    asset = choose(client, current(client, asset), first).json()
    second = approved(client, asset)
    asset = current(client, asset)
    url = path(pid) + "/dependency-impact?asset_version_id=" + first["id"]
    old_preview = client.get(url).json()
    with client.app.state.session_factory.begin() as db:
        output = produce(
            db,
            pid,
            "SHOT_IMAGE",
            (db.get(VisualAsset, UUID(asset["id"])), db.get(AssetVersion, UUID(first["id"]))),
        )
        output_id = output[1].id
    endpoint = versions(asset) + "/" + second["id"] + "/select"
    body = {
        "expected_revision": asset["revision"],
        "expected_lock_version": asset["lock_version"],
        "impact_digest": old_preview["digest"],
    }
    response = client.post(endpoint, json=body)
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "DEPENDENCY_IMPACT_CHANGED"
    )
    assert current(client, asset) == asset
    with client.app.state.session_factory() as db:
        assert not db.get(AssetVersion, output_id).stale
    fresh = client.get(url).json()
    assert fresh["digest"] != old_preview["digest"]
    assert client.post(endpoint, json={**body, "impact_digest": fresh["digest"]}).status_code == 200
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, output_id).stale


def test_actual_text_approval_preview_uses_changed_dialogue_not_whole_script(client, episode):
    prefix = f"/api/v1/episodes/{episode['id']}/scripts"
    content = MockTextProvider().script("雨夜", {}).model_dump(mode="json")
    content["scenes"][0]["dialogue"] = [
        {
            "line_id": str(uuid4()),
            "speaker": content["characters"][0],
            "text": "原台词",
            "line_type": "dialogue",
            "emotion": "",
        }
    ]
    saved = client.post(prefix, json={"expected_version_no": 0, "content": content})
    assert saved.status_code == 201, saved.text
    first = saved.json()
    assert (
        client.post(prefix + f"/{first['id']}/review", json={"decision": "APPROVE"}).status_code
        == 200
    )
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = db.get(AssetVersion, UUID(first["id"]))
        pair = (db.get(VisualAsset, source.asset_id), source)
        audio = produce(db, pid, "AUDIO", pair)
        produce(db, pid, "SHOT_IMAGE", pair)
        audio_id = str(audio[1].id)
    modified = deepcopy(content)
    modified["scenes"][0]["dialogue"][0]["text"] = "新台词"
    second = client.post(prefix, json={"expected_version_no": 1, "content": modified}).json()
    url = prefix + f"/{second['id']}"
    result = client.get(url + "/dependency-impact").json()
    assert [x["asset_version_id"] for x in result["affected"]] == [audio_id]
    response = client.post(
        url + "/review", json={"decision": "APPROVE", "impact_digest": result["digest"]}
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("role", ["viewer", "editor", "outsider"])
def test_preview_permission_and_foreign_identity_are_read_only(client, role):
    owner = register(client)
    project = create_project(client)
    asset = create(client, project["id"])
    version = approved(client, asset)
    member = register(client, "impact-member@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    if role != "outsider":
        assert (
            client.post(
                path(project["id"]) + "/members",
                json={"email": member["user"]["email"], "role": role},
            ).status_code
            == 201
        )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    response = client.get(
        path(project["id"]) + "/dependency-impact", params={"asset_version_id": version["id"]}
    )
    assert response.status_code == (404 if role == "outsider" else 200)
    assert (
        client.get(
            path(project["id"]) + "/dependency-impact", params={"asset_version_id": str(uuid4())}
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "patch,status",
    [
        ({"expected_revision": 99}, 409),
        ({"change_kind": "DIALOGUE"}, 422),
        ({"source_asset_version_id": str(uuid4())}, 404),
        ({"selectors": ["x", "x"]}, 422),
    ],
)
def test_preview_validates_source_scope_and_revision(client, episode, patch, status):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = registry_version(db, pid)
        body = {**change(source[1]), **patch}
    assert (
        client.post(path(pid) + "/dependencies/impact", json={"changes": [body]}).status_code
        == status
    )
    assert client.post(path(pid) + "/dependencies/impact", json={"changes": []}).status_code == 422
