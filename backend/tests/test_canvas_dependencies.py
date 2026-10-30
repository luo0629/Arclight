"""DAG-06: authoritative project versions, exact edges and private previews."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import select
from test_asset_selection import approved, choose, current
from test_asset_versions import image_bytes, private_objects, save, state, versions  # noqa: F401
from test_assets import create
from test_collaboration import create_project, register
from test_dependency_manifests import capture, source_input
from test_dependency_validation import registry_version

from arclight.api import canvas
from arclight.persistence.models import AssetDependency, AssetVersion


def path(episode):
    return f"/api/v1/episodes/{episode['id']}/canvas"


def test_canvas_exact_candidates_stale_selection_and_immutable_history(client, episode):
    source = create(client, episode["project_id"])
    reference = approved(client, source)
    choose(client, current(client, source), reference)
    target = create(client, episode["project_id"], "SCENE")
    manifest = capture(client, target, [source_input(reference)]).json()
    output = save(client, target, dependency_manifest_id=manifest["id"])
    later = approved(client, current(client, source))
    graph = client.get(path(episode)).json()
    nodes = {node["resource_id"]: node for node in graph["nodes"] if "resource_id" in node}
    assert nodes[source["id"]]["kind"] == "asset"
    assert nodes[reference["id"]]["current"]
    assert not nodes[later["id"]]["current"]
    assert nodes[output["id"]]["status"] == "DRAFT"
    assert nodes[output["id"]]["dependency_manifest_id"] == manifest["id"]
    assert nodes[output["id"]]["checksum"] == output["checksum"]
    edge = next(edge for edge in graph["edges"] if edge.get("relation") == "dependency")
    assert edge["source"] == nodes[reference["id"]]["id"]
    assert edge["target"] == nodes[output["id"]]["id"]
    assert edge["dependency_type"] == "REFERENCE_DEPENDENCY"
    assert edge["dependency_manifest_id"] == manifest["id"]
    assert edge["label"] == "参考依赖"
    assert client.get("/api/v1" + nodes[output["id"]]["preview_path"]).content == image_bytes()
    assert "object_key" not in str(graph) and "content_base64" not in str(graph)
    assert state(client, source, reference, "STALE").status_code == 200
    stale = client.get(path(episode)).json()
    stale_nodes = {node["id"]: node for node in stale["nodes"]}
    assert stale_nodes[nodes[output["id"]]["id"]]["stale"]
    assert stale_nodes[nodes[output["id"]]["id"]]["status"] == "STALE"
    assert stale["edges"] == graph["edges"]
    assert client.get("/api/v1" + nodes[reference["id"]]["preview_path"]).content == image_bytes()


def test_layout_cannot_add_edges_or_foreign_versions_and_never_changes_dependencies(
    client, episode
):
    source = create(client, episode["project_id"])
    reference = approved(client, source)
    target = create(client, episode["project_id"], "SCENE")
    manifest = capture(client, target, [source_input(reference)]).json()
    output = save(client, target, dependency_manifest_id=manifest["id"])
    initial = client.get(path(episode)).json()
    body = {
        "expected_revision": 0,
        "positions": {"asset-version:" + output["id"]: {"x": 15, "y": -50}},
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    assert client.put(path(episode) + "/layout", json={**body, "edges": []}).status_code == 422
    with client.app.state.session_factory.begin() as db:
        foreign = create_project(client)
        _, version = registry_version(db, UUID(foreign["id"]))
        foreign_id = str(version.id)
    assert (
        client.put(
            path(episode) + "/layout",
            json={**body, "positions": {"asset-version:" + foreign_id: {"x": 1, "y": 2}}},
        ).status_code
        == 422
    )
    assert client.put(path(episode) + "/layout", json=body).status_code == 200
    after = client.get(path(episode)).json()
    assert after["layout"]["positions"] == body["positions"]
    assert after["nodes"] == initial["nodes"] and after["edges"] == initial["edges"]


@pytest.mark.parametrize("member", [False, True])
def test_canvas_and_preview_share_project_read_permissions(client, member):
    owner = register(client)
    project = create_project(client)
    episode = client.post(
        f"/api/v1/projects/{project['id']}/episodes", json={"title": "测试", "episode_no": 1}
    ).json()
    source = create(client, project["id"])
    version = save(client, source)
    node = next(
        n
        for n in client.get(path(episode)).json()["nodes"]
        if n.get("resource_id") == version["id"]
    )
    user = register(client, "canvas-reader@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    if member:
        assert (
            client.post(
                f"/api/v1/projects/{project['id']}/members",
                json={"email": user["user"]["email"], "role": "viewer"},
            ).status_code
            == 201
        )
    client.headers["Authorization"] = "Bearer " + user["access_token"]
    expected = 200 if member else 404
    assert client.get(path(episode)).status_code == expected
    assert client.get("/api/v1" + node["preview_path"]).status_code == expected
    assert client.put(
        path(episode) + "/layout",
        json={"expected_revision": 0, "positions": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}},
    ).status_code == (403 if member else 404)


def test_cross_project_legacy_edge_is_never_exposed_and_external_objects_have_no_preview(
    client, episode
):
    foreign = create_project(client)
    with client.app.state.session_factory.begin() as db:
        _, local = registry_version(db, UUID(episode["project_id"]))
        _, other = registry_version(db, UUID(foreign["id"]))
        db.add(
            AssetDependency(
                source_asset_version_id=other.id,
                target_asset_version_id=local.id,
                dependency_type="REFERENCE_DEPENDENCY",
            )
        )
        local_id, foreign_id = str(local.id), str(other.id)
    response = client.get(path(episode))
    assert response.status_code == 200, response.text
    graph = response.json()
    assert foreign_id not in str(graph)
    assert not [edge for edge in graph["edges"] if edge.get("relation") == "dependency"]
    assert (
        next(node for node in graph["nodes"] if node.get("resource_id") == local_id)["preview_path"]
        is None
    )


def test_graph_limit_fails_explicitly_instead_of_truncating_exact_dependencies(
    client, episode, monkeypatch
):
    source = create(client, episode["project_id"])
    save(client, source)
    monkeypatch.setattr(canvas, "MAX_CANVAS_NODES", 2)
    response = client.get(path(episode))
    assert response.status_code == 409 and response.json()["error"]["code"] == "CANVAS_TOO_LARGE"
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(AssetVersion)))) == 1


def test_sqlite_canvas_keeps_one_snapshot_while_a_worker_commits(client, episode, monkeypatch):
    if client.app.state.engine.dialect.name != "sqlite":
        pytest.skip("SQLite read snapshots; PostgreSQL uses the shared project lock")
    project_id = UUID(episode["project_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        source, source_version = registry_version(db, project_id)
        target, _ = registry_version(db, project_id)
        source_id, target_id = source_version.id, target.id
    ready, committed = Event(), Event()
    original = canvas.query_dependencies
    pending = []

    def write():
        with factory.begin() as db:
            version = AssetVersion(
                asset_id=target_id,
                version_no=2,
                object_key="test:concurrent-result",
                checksum="b" * 64,
                metadata_json={},
                created_by="service-owner",
            )
            db.add(version)
            db.flush()
            db.add(
                AssetDependency(
                    source_asset_version_id=source_id,
                    target_asset_version_id=version.id,
                    dependency_type="REFERENCE_DEPENDENCY",
                )
            )
            db.flush()
            version_id = version.id
            ready.set()
        committed.set()
        return version_id

    with ThreadPoolExecutor(max_workers=1) as pool:

        def during_read(db, *args, **kwargs):
            pending.append(pool.submit(write))
            assert ready.wait(10)
            # A real read snapshot prevents a later commit from entering only the edge query.
            assert not committed.wait(0.5)
            return original(db, *args, **kwargs)

        monkeypatch.setattr(canvas, "query_dependencies", during_read)
        response = client.get(path(episode))
        assert response.status_code == 200, response.text
        version_id = pending[0].result(timeout=10)
    assert str(version_id) not in str(response.json())
    monkeypatch.setattr(canvas, "query_dependencies", original)
    next_graph = client.get(path(episode)).json()
    assert str(version_id) in str(next_graph)
    identities = {node["id"] for node in next_graph["nodes"]}
    assert all(
        edge["source"] in identities and edge["target"] in identities
        for edge in next_graph["edges"]
    )
