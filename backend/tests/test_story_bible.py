"""STORY-02: narrative identity, authorization, exact references and immutable context."""

from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import inspect
from test_api_workflow import advance, start
from test_collaboration import register
from test_text_contract_v2 import make_asset

from arclight.persistence.models import AssetVersion, WorkflowRun
from arclight.providers.text import MockTextProvider


def path(episode):
    return f"/api/v1/projects/{episode['project_id']}/story-entries"


def create(client, episode, kind="WORLD", **values):
    response = client.post(path(episode), json={"kind": kind, "name": kind, **values})
    assert response.status_code == 201, response.text
    return response.json()


def replace(client, episode, row, **values):
    body = {k: row[k] for k in ("kind", "name", "description", "rules", "relations", "asset")}
    return client.put(
        path(episode) + "/" + row["id"],
        json={**body, "expected_revision": row["revision"], **values},
    )


def archive(client, episode, row, archived=True):
    return client.patch(
        path(episode) + "/" + row["id"],
        json={"expected_revision": row["revision"], "archived": archived},
    )


def test_crud_revision_pagination_and_stable_identity(client, episode):
    first = create(client, episode, rules=["不能飞行", "不能复活"])
    assert first["revision"] == 0 and first["rules"] == ["不能飞行", "不能复活"]
    for kind in ("CHARACTER", "LOCATION", "PROP", "SCENE"):
        create(client, episode, kind)
    rows = client.get(path(episode) + "?limit=2&offset=0").json()
    assert len(rows) == 2
    assert rows[0]["id"] == first["id"]
    assert not {r["id"] for r in rows} & {
        r["id"] for r in client.get(path(episode) + "?limit=3&offset=2").json()
    }
    edited = replace(client, episode, first, name="世界修订", rules=[]).json()
    assert edited["id"] == first["id"] and edited["revision"] == 1
    assert edited["rules"] == []
    assert replace(client, episode, first, name="过期草稿").status_code == 409
    assert replace(client, episode, edited, kind="PROP").status_code == 422
    assert client.get(path(episode) + "/" + first["id"]).json()["name"] == "世界修订"
    assert client.delete(path(episode) + "/" + first["id"]).status_code == 405
    assert client.get(path(episode) + "?limit=0").status_code == 422


@pytest.mark.parametrize(
    "values",
    [
        {"name": " "},
        {"kind": "COSTUME"},
        {"rules": ["重复", "重复"]},
        {"rules": ["a"] * 11},
        {"rules": ["a" * 501]},
        {"description": "a" * 2001},
        {"unknown": True},
        {"rules": [" "]},
    ],
)
def test_invalid_input_is_atomic(client, episode, values):
    response = client.post(path(episode), json={"kind": "WORLD", "name": "测试", **values})
    assert response.status_code == 422
    assert client.get(path(episode)).json() == []


def test_typed_links_archive_dependencies_and_restore(client, episode):
    location = create(client, episode, "LOCATION")
    prop = create(client, episode, "PROP")
    scene = create(
        client,
        episode,
        "SCENE",
        relations=[
            {"relation": "located_in", "target_id": location["id"]},
            {"relation": "uses", "target_id": prop["id"]},
        ],
    )
    character = create(
        client,
        episode,
        "CHARACTER",
        relations=[
            {"relation": "appears_in", "target_id": scene["id"]},
            {"relation": "located_in", "target_id": location["id"]},
        ],
    )
    world = create(
        client, episode, relations=[{"relation": "governs", "target_id": character["id"]}]
    )
    assert archive(client, episode, location).json()["error"]["code"] == "STORY_ENTRY_IN_USE"
    for row in (world, character, scene):
        assert archive(client, episode, row).status_code == 200
    archived = archive(client, episode, location).json()
    assert archived["archived"] is True
    assert replace(client, episode, archived).status_code == 409
    old_scene = client.get(path(episode) + "/" + scene["id"]).json()
    assert archive(client, episode, old_scene, False).status_code == 422
    assert archive(client, episode, archived, False).status_code == 200
    assert archive(client, episode, old_scene, False).status_code == 200
    assert len(client.get(path(episode)).json()) == 5


@pytest.mark.parametrize(
    "relation,target_kind",
    [
        ("governs", "LOCATION"),
        ("located_in", "PROP"),
        ("appears_in", "CHARACTER"),
        ("uses", "SCENE"),
    ],
)
def test_wrong_relation_type(client, episode, relation, target_kind):
    target = create(client, episode, target_kind)
    response = client.post(
        path(episode),
        json={
            "kind": "CHARACTER",
            "name": "角色",
            "relations": [{"relation": relation, "target_id": target["id"]}],
        },
    )
    assert response.status_code == 422


def test_unknown_cross_project_self_archived_and_duplicate_links(client, episode):
    other = client.post("/api/v1/projects", json={"name": "另一个项目"}).json()
    outside = create(client, {"project_id": other["id"]}, "LOCATION")
    location = create(client, episode, "LOCATION")
    character = create(client, episode, "CHARACTER")
    archived = archive(client, episode, location).json()
    for target in (str(uuid4()), outside["id"], archived["id"], character["id"]):
        assert (
            replace(
                client,
                episode,
                character,
                relations=[{"relation": "located_in", "target_id": target}],
            ).status_code
            == 422
        )
    assert client.get(path(episode) + "/" + outside["id"]).status_code == 404
    restored = archive(client, episode, archived, False).json()
    link = {"relation": "located_in", "target_id": restored["id"]}
    assert replace(client, episode, character, relations=[link, link]).status_code == 422


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_membership_permissions(client, role):
    owner = register(client, "story-owner@example.com")
    project = client.post("/api/v1/projects", json={"name": "设定权限"}).json()
    episode = {"project_id": project["id"]}
    row = create(client, episode)
    other = register(client, "story-other@example.com")
    headers = {"Authorization": "Bearer " + owner["access_token"]}
    if role != "outsider":
        assert (
            client.post(
                f"/api/v1/projects/{project['id']}/members",
                headers=headers,
                json={"email": other["user"]["email"], "role": role},
            ).status_code
            == 201
        )
    expected = 200 if role != "outsider" else 404
    assert client.get(path(episode)).status_code == expected
    assert replace(client, episode, row, name="修改").status_code == (
        200 if role == "editor" else 404 if role == "outsider" else 403
    )
    assert client.post(path(episode), json={"kind": "PROP", "name": "物品"}).status_code == (
        201 if role == "editor" else 404 if role == "outsider" else 403
    )
    assert archive(client, episode, row).status_code == (
        409 if role == "editor" else 404 if role == "outsider" else 403
    )


def test_archived_project_and_active_run_block_all_mutations(client, episode):
    row = create(client, episode)
    run = start(client, episode).json()
    assert replace(client, episode, row).status_code == 409
    assert archive(client, episode, row).status_code == 409
    assert client.post(path(episode), json={"kind": "WORLD", "name": "blocked"}).status_code == 409
    client.post(f"/api/v1/runs/{run['run_id']}/cancel")
    assert (
        client.patch(
            f"/api/v1/projects/{episode['project_id']}", json={"archived": True}
        ).status_code
        == 200
    )
    assert client.get(path(episode)).status_code == 200
    assert replace(client, episode, row).status_code == 409
    assert archive(client, episode, row).status_code == 409


def test_context_bounds_reject_atomically_without_truncation(client, episode, monkeypatch):
    monkeypatch.setattr("arclight.application.story_context.MAX_STORY_ENTRIES", 1)
    row = create(client, episode)
    assert client.post(path(episode), json={"kind": "WORLD", "name": "overflow"}).status_code == 422
    assert len(client.get(path(episode)).json()) == 1
    monkeypatch.setattr("arclight.application.story_context.MAX_STORY_CONTEXT_CHARS", 50)
    assert replace(client, episode, row, description="overflow").status_code == 422
    persisted = client.get(path(episode) + "/" + row["id"]).json()
    assert persisted["description"] == ""
    assert persisted["revision"] == row["revision"]
    assert persisted["id"] == row["id"]
    assert start(client, episode).status_code == 422
    assert client.get(f"/api/v1/episodes/{episode['id']}/runs").json() == []


def test_snapshot_retry_idempotency_and_new_runs(client, episode, settings, monkeypatch):
    row = create(client, episode, rules=["旧规则"])
    requests = []

    class Recording(MockTextProvider):
        def script(
            self, story, style, duration_ms=30000, *, story_settings=None, story_context=None
        ):
            requests.append(story_context)
            if len(requests) == 1:
                raise RuntimeError("transient fixture failure")
            return super().script(story, style, duration_ms)

    monkeypatch.setattr("arclight.application.runtime.build_provider", lambda _: Recording())
    run_id = start(client, episode, key="story-context").json()["run_id"]
    assert advance(client, settings, run_id)["status"] == "FAILED"
    assert replace(client, episode, row, rules=["新规则"]).status_code == 200
    assert start(client, episode, key="story-context").json()["run_id"] == run_id
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
    assert requests[0] == requests[1]
    assert requests[0]["entries"][0]["rules"] == ["旧规则"]
    assert requests[0]["entries"][0]["id"] == row["id"]
    client.post(f"/api/v1/runs/{run_id}/cancel")
    new_id = start(client, episode, key="new-context").json()["run_id"]
    with client.app.state.session_factory() as db:
        snapshot = db.get(WorkflowRun, UUID(new_id)).input_json["story_context"]
        assert snapshot["entries"][0]["rules"] == ["新规则"]
        assert snapshot["project_id"] == episode["project_id"]


def test_exact_asset_binding_and_stale_revalidation(client, episode):
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, episode["project_id"], "CHARACTER")
    row = create(client, episode, "CHARACTER", asset=reference)
    assert row["asset"] == reference
    assert (
        client.post(
            path(episode), json={"kind": "WORLD", "name": "世界", "asset": reference}
        ).status_code
        == 422
    )
    assert (
        client.post(
            path(episode), json={"kind": "PROP", "name": "错类", "asset": reference}
        ).status_code
        == 422
    )
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(reference["asset_version_id"])).stale = True
    assert replace(client, episode, row).json()["error"]["code"] == "ASSET_NOT_USABLE"
    assert start(client, episode).status_code == 422
    archived = archive(client, episode, row).json()
    assert archived["archived"] is True
    assert archive(client, episode, archived, False).status_code == 422
    assert start(client, episode).status_code == 202


@pytest.mark.legacy_schema
def test_migration_0005_preserves_0004_and_guards_even_archived_entries(client, episode, migrated):
    command.downgrade(migrated, "0004")
    assert "story_entries" not in inspect(client.app.state.engine).get_table_names()
    command.upgrade(migrated, "0018")
    assert (
        client.get(f"/api/v1/projects/{episode['project_id']}").json()["id"]
        == episode["project_id"]
    )
    row = create(client, episode)
    assert archive(client, episode, row).status_code == 200
    with pytest.raises(RuntimeError, match="Story entries exist"):
        command.downgrade(migrated, "0004")
    # A failed multi-revision downgrade may already have removed newer columns.
    # Restore the current application schema before exercising the current ORM.
    command.upgrade(migrated, "0018")
    assert client.get(path(episode)).json()[0]["archived"] is True
    # Verify the current schema after the historical preservation assertions.
    command.upgrade(migrated, "head")
    command.check(migrated)
