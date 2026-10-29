"""DAG-05: explicit new inputs, immutable candidates and selective director runs."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from test_asset_selection import approved, choose, current, restore
from test_asset_versions import private_objects, save, state, versions  # noqa: F401
from test_assets import create
from test_collaboration import create_project, register
from test_dependency_manifests import capture, derived, path, source_input

from arclight.agents.contracts import ScriptContent
from arclight.application.runtime import execute_run
from arclight.persistence.models import AssetVersion, WorkflowRun
from arclight.providers.text import MockTextProvider


def prepared(client, pid):
    source = create(client, pid)
    first = approved(client, source)
    source = choose(client, current(client, source), first).json()
    stable = create(client, pid, "PROP")
    stable_version = approved(client, stable)
    target = create(client, pid, "SCENE")
    original = capture(client, target, [source_input(first), source_input(stable_version)]).json()
    output = save(client, target, dependency_manifest_id=original["id"])
    fresh = approved(client, current(client, source))
    assert choose(client, current(client, source), fresh).status_code == 200
    output = client.get(versions(target) + "/" + output["id"]).json()
    assert output["stale"]
    return source, first, stable, stable_version, target, original, output, fresh


def rebuild(client, target, output, replacements, **extra):
    return client.post(
        versions(target) + f"/{output['id']}/rebuild-manifest",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": output["revision"],
            "replacements": replacements,
            **extra,
        },
    )


def test_reconfirm_reuses_unchanged_input_and_keeps_all_old_snapshots(client, episode):
    source, first, stable, unchanged, target, old, output, fresh = prepared(
        client, episode["project_id"]
    )
    base = versions(target) + "/" + output["id"]
    inputs = client.get(base + "/rebuild-inputs")
    assert inputs.status_code == 200, inputs.text
    assert inputs.json()["inputs"][0]["current_version_id"] == fresh["id"]
    key = str(uuid4())
    response = rebuild(client, target, output, {first["id"]: fresh["id"]}, creation_key=key)
    assert response.status_code == 201, response.text
    manifest = response.json()
    assert (
        rebuild(client, target, output, {first["id"]: fresh["id"]}, creation_key=key).json()
        == manifest
    )
    assert rebuild(client, target, output, {}, creation_key=key).status_code == 409
    assert {x["source_asset_version_id"] for x in manifest["snapshot"]["inputs"]} == {
        fresh["id"],
        unchanged["id"],
    }
    assert manifest["snapshot"]["context"]["previous_manifest_id"] == old["id"]
    new = restore(client, current(client, target), output, dependency_manifest_id=manifest["id"])
    assert new.status_code == 201, new.text
    new = new.json()
    assert new["id"] != output["id"] and new["checksum"] == output["checksum"]
    assert new["status"] == "DRAFT" and not new["stale"]
    assert state(client, target, new, "APPROVED").status_code == 409
    ready = state(client, target, new, "READY_FOR_REVIEW").json()
    assert state(client, target, ready, "APPROVED").status_code == 200
    assert current(client, target)["current_version_id"] is None
    assert client.get(base).json() == output
    assert (
        client.get(path(episode["project_id"]) + "/dependency-manifests/" + old["id"]).json() == old
    )
    assert client.get(versions(stable) + "/" + unchanged["id"]).json() == unchanged
    edges = client.get(
        path(episode["project_id"]) + "/dependencies", params={"target_version_id": new["id"]}
    ).json()
    assert {x["source_asset_version_id"] for x in edges} == {fresh["id"], unchanged["id"]}


def test_derived_restore_cannot_drop_dependencies(client, episode):
    _, _, target, _, output = derived(client, episode["project_id"])
    response = restore(client, current(client, target), output)
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "DEPENDENCY_MANIFEST_REQUIRED"
    )
    assert len(client.get(versions(target)).json()) == 1


@pytest.mark.parametrize("invalid", ["unrelated", "foreign", "unknown_old", "stale", "revision"])
def test_rebuild_rejects_invalid_mapping_atomically(client, episode, invalid):
    source, first, target, _, output = derived(client, episode["project_id"])
    extra = {}
    other = create(client, episode["project_id"])
    replacement = approved(client, other)
    mapping = {first["id"]: replacement["id"]}
    if invalid == "foreign":
        foreign = create_project(client)
        replacement = approved(client, create(client, foreign["id"]))
        mapping = {first["id"]: replacement["id"]}
    elif invalid == "unknown_old":
        mapping = {str(uuid4()): first["id"]}
    elif invalid == "stale":
        state(client, source, first, "STALE")
        output = client.get(versions(target) + "/" + output["id"]).json()
        mapping = {}
    elif invalid == "revision":
        extra["expected_revision"] = 999
    response = rebuild(client, target, output, mapping, **extra)
    assert response.status_code in {404, 409, 422}, response.text
    assert len(client.get(versions(target)).json()) == 1


def test_late_reconfirmed_candidate_stays_stale_after_input_changes(client, episode):
    source, reference, target, _, output = derived(client, episode["project_id"])
    manifest = rebuild(client, target, output, {}).json()
    state(client, source, reference, "STALE")
    result = restore(client, current(client, target), output, dependency_manifest_id=manifest["id"])
    assert result.status_code == 201, result.text
    assert result.json()["stale"] and result.json()["status"] == "STALE"


def test_selective_director_rebuild_creates_new_review_without_touching_history(
    client, episode, settings
):
    prefix = f"/api/v1/episodes/{episode['id']}"
    content = MockTextProvider().script("旧故事", {}).model_dump(mode="json")
    script = client.post(
        prefix + "/scripts", json={"expected_version_no": 0, "content": content}
    ).json()
    assert (
        client.post(
            prefix + f"/scripts/{script['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    board_content = (
        MockTextProvider()
        .storyboard(ScriptContent.model_validate(content), 30000)
        .model_dump(mode="json")
    )
    board = client.post(
        prefix + "/storyboards",
        json={
            "expected_version_no": 0,
            "script_version_id": script["id"],
            "content": board_content,
        },
    ).json()
    assert (
        client.post(
            prefix + f"/storyboards/{board['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    changed = deepcopy(content)
    changed["summary"] = "改变故事场景"
    next_script = client.post(
        prefix + "/scripts", json={"expected_version_no": 1, "content": changed}
    ).json()
    assert (
        client.post(
            prefix + f"/scripts/{next_script['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    body = {"script_version_id": next_script["id"], "rebuild_from_version_id": board["id"]}
    response = client.post(
        prefix + "/storyboard-runs", json=body, headers={"Idempotency-Key": "selective-rebuild"}
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert (
        client.post(
            prefix + "/storyboard-runs", json=body, headers={"Idempotency-Key": "selective-rebuild"}
        ).json()["run_id"]
        == run_id
    )
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    pending = client.get("/api/v1/runs/" + run_id).json()
    assert (
        pending["status"] == "WAITING_REVIEW"
        and pending["pending_review"]["version_id"] != board["id"]
    )
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        assert run.input_json["rebuild_from_version_id"] == board["id"]
        assert run.input_json["script_version_id"] == next_script["id"]
        assert db.get(AssetVersion, UUID(board["id"])).stale
    boards = client.get(prefix + "/storyboards").json()
    assert len(boards) == 2 and boards[1]["content_json"] == board["content_json"]
    assert boards[1]["status"] == "STALE" and boards[0]["status"] == "READY_FOR_REVIEW"
    assert boards[0]["script_version_id"] == next_script["id"]


@pytest.mark.parametrize("role", ["viewer", "outsider"])
def test_rebuild_permissions(client, role):
    owner = register(client)
    project = create_project(client)
    _, _, target, _, output = derived(client, project["id"])
    member = register(client, "rebuild-member@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    if role == "viewer":
        assert (
            client.post(
                path(project["id"]) + "/members",
                json={"email": member["user"]["email"], "role": role},
            ).status_code
            == 201
        )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert rebuild(client, target, output, {}).status_code == (403 if role == "viewer" else 404)
