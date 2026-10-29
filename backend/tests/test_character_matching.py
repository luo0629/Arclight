"""STORY-09: exact human decisions, identity collisions and immutable source evidence."""

from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, select
from test_api_workflow import advance, start
from test_collaboration import register
from test_concurrency import race, require_postgres
from test_story_analysis import content, execute
from test_story_analysis import start as start_analysis
from test_story_bible import archive, create, replace
from test_story_bible import path as entries_path
from test_text_contract_v2 import make_asset

from arclight.agents.story_analysis import StoryAnalysisContent
from arclight.persistence.models import (
    CharacterConfirmation,
    Script,
    StoryEntry,
    VisualAsset,
    WorkflowRun,
)
from arclight.providers.text import MockTextProvider


def script(client, episode, names=None, **values):
    value = MockTextProvider().script("故事", {}).model_dump(mode="json")
    value.update(characters=names or ["主角"], **values)
    versions = client.get(f"/api/v1/episodes/{episode['id']}/scripts").json()
    result = client.post(
        f"/api/v1/episodes/{episode['id']}/scripts",
        json={
            "expected_version_no": max((row["version_no"] for row in versions), default=0),
            "content": value,
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


def path(episode, source, kind="scripts"):
    return f"/api/v1/episodes/{episode['id']}/{kind}/{source['id']}/character-matches"


def body(client, url, name="主角", entry_id=None, **values):
    preview = client.get(url)
    assert preview.status_code == 200, preview.text
    return {
        "source_name": name,
        "expected_fingerprint": preview.json()["fingerprint"],
        "decision": "REUSE" if entry_id else "CREATE",
        "entry_id": entry_id,
        **values,
    }


def test_preview_never_writes_and_explicit_create_is_idempotent(client, episode):
    source = script(client, episode)
    with client.app.state.session_factory() as db:
        original_assets = set(db.scalars(select(VisualAsset.id)))
    url = path(episode, source)
    request = body(client, url)
    assert client.get(url).json()["candidates"][0]["matches"] == []
    assert client.get(entries_path(episode)).json() == []
    response = client.post(url, json=request)
    assert response.status_code == 200, response.text
    confirmed = response.json()
    assert confirmed["entry"]["name"] == "主角" and confirmed["entry"]["revision"] == 0
    assert confirmed["source_name"] == "主角" and len(confirmed["source_sha256"]) == 64
    assert client.post(url, json=request).json() == confirmed
    assert client.post(url, json={**request, "expected_fingerprint": "a" * 64}).json() == confirmed
    assert client.get(url).json()["candidates"][0]["confirmation"] == confirmed
    assert len(client.get(entries_path(episode)).json()) == 1
    after = client.get(f"/api/v1/episodes/{episode['id']}/scripts").json()[0]
    assert after["content_json"] == source["content_json"] and after["status"] == source["status"]
    with client.app.state.session_factory() as db:
        # The script is now a graph node. Matching must create no new assets of
        # any kind, including visual characters or duplicate text identities.
        assert set(db.scalars(select(VisualAsset.id))) == original_assets
        assert db.scalar(select(func.count()).select_from(CharacterConfirmation)) == 1


def test_aliases_normalize_and_share_one_identity_without_changing_script(client, episode):
    entry = create(client, episode, "CHARACTER", name="Alice", aliases=["小雨", "雨姐"])
    source = script(client, episode, [" ＡＬＩＣＥ ", "小雨", "雨姐", "旁白"])
    url = path(episode, source)
    result = client.get(url).json()
    assert len(result["candidates"]) == 3
    for item in result["candidates"]:
        assert item["matches"] == [entry["id"]]
        response = client.post(url, json=body(client, url, item["name"], entry["id"]))
        assert response.status_code == 200, response.text
    assert len(client.get(entries_path(episode)).json()) == 1
    after = client.get(f"/api/v1/episodes/{episode['id']}/scripts").json()[0]
    assert after["content_json"] == source["content_json"] and after["status"] == source["status"]


@pytest.mark.parametrize(
    "values",
    [
        {"aliases": ["ＡＬＩＣＥ"]},
        {"aliases": ["小雨", " 小雨 "]},
        {"aliases": [" "]},
        {"aliases": ["a" * 101]},
        {"aliases": [str(i) for i in range(11)]},
        {"kind": "PROP", "aliases": ["别名"]},
    ],
)
def test_invalid_alias_contract(client, episode, values):
    response = client.post(
        entries_path(episode), json={"kind": "CHARACTER", "name": "Alice", **values}
    )
    assert response.status_code == 422
    assert client.get(entries_path(episode)).json() == []


def test_collision_checks_cover_rename_aliases_archived_and_restore(client, episode):
    first = create(client, episode, "CHARACTER", name="Alice", aliases=["小雨"])
    second = create(client, episode, "CHARACTER", name="阿青")
    assert replace(client, episode, second, name="Ａlice").status_code == 409
    assert replace(client, episode, second, aliases=["小雨"]).status_code == 409
    archived = archive(client, episode, first).json()
    assert (
        client.post(entries_path(episode), json={"kind": "CHARACTER", "name": "小雨"}).status_code
        == 409
    )
    source = script(client, episode, ["小雨"])
    url = path(episode, source)
    assert client.get(url).json()["entries"][0]
    assert client.post(url, json=body(client, url, "小雨")).status_code == 409
    assert client.post(url, json=body(client, url, "小雨", first["id"])).status_code == 422
    assert archive(client, episode, archived, False).status_code == 200
    assert client.post(url, json=body(client, url, "小雨", first["id"])).status_code == 200


def test_stale_preview_cannot_create_after_alias_added(client, episode):
    entry = create(client, episode, "CHARACTER", name="阿青")
    source = script(client, episode, ["小雨"])
    url = path(episode, source)
    pending = body(client, url, "小雨")
    assert replace(client, episode, entry, aliases=["小雨"]).status_code == 200
    assert client.post(url, json=pending).json()["error"]["code"] == "STALE_CHARACTER_MATCH"
    assert (
        client.post(url, json=body(client, url, "小雨")).json()["error"]["code"]
        == "CHARACTER_NAME_CONFLICT"
    )
    assert len(client.get(entries_path(episode)).json()) == 1


def test_confirmation_snapshot_survives_changes_and_does_not_transfer_to_new_version(
    client, episode
):
    entry = create(client, episode, "CHARACTER", name="阿青", aliases=["小雨"])
    source = script(client, episode, ["小雨"])
    url = path(episode, source)
    confirmed = client.post(url, json=body(client, url, "小雨", entry["id"])).json()
    changed = replace(client, episode, entry, name="阿青修订", aliases=["小雨"]).json()
    assert archive(client, episode, changed).status_code == 200
    assert client.get(url).json()["candidates"][0]["confirmation"] == confirmed
    assert confirmed["entry"]["name"] == "阿青" and confirmed["entry"]["revision"] == 0
    newer = script(client, episode, ["小雨"])
    assert client.get(path(episode, newer)).json()["candidates"][0]["confirmation"] is None
    assert client.get(url).json()["entries"][0]["archived"] is True
    assert client.post(url, json=body(client, url, "小雨")).status_code == 409


def test_analysis_matches_source_aliases_and_new_identity_requires_explicit_confirmation(
    client, episode, settings, monkeypatch
):
    monkeypatch.setattr(
        MockTextProvider, "analyze", lambda *args: StoryAnalysisContent.model_validate(content())
    )
    run = start_analysis(client, episode).json()["run_id"]
    execute(client, settings, run)
    source = client.get(f"/api/v1/episodes/{episode['id']}/story-analyses").json()[0]
    url = path(episode, source, "story-analyses")
    pending = body(client, url, "阿青")
    assert client.get(entries_path(episode)).json() == []
    result = client.post(url, json=pending)
    assert result.status_code == 200, result.text
    assert result.json()["entry"]["aliases"] == ["小青"]
    # A distinct source version suggests the prior identity but creates nothing.
    run = start_analysis(client, episode, key="second").json()["run_id"]
    execute(client, settings, run)
    newer = client.get(f"/api/v1/episodes/{episode['id']}/story-analyses").json()[0]
    match = client.get(path(episode, newer, "story-analyses")).json()["candidates"][0]
    assert match["matches"] == [result.json()["entry"]["id"]] and match["confirmation"] is None
    assert len(client.get(entries_path(episode)).json()) == 1


def test_ambiguous_legacy_names_are_not_silently_chosen(client, episode):
    first = create(client, episode, "CHARACTER", name="阿青")
    with client.app.state.session_factory.begin() as db:
        legacy = StoryEntry(
            project_id=UUID(episode["project_id"]),
            kind="CHARACTER",
            name="阿青",
            data_json={},
            created_by="service-owner",
        )
        db.add(legacy)
        db.flush()
        legacy_id = str(legacy.id)
    source = script(client, episode, ["阿青"])
    url = path(episode, source)
    assert set(client.get(url).json()["candidates"][0]["matches"]) == {first["id"], legacy_id}
    assert client.post(url, json=body(client, url, "阿青")).status_code == 409
    assert (
        client.post(url, json=body(client, url, "阿青", legacy_id)).json()["entry"]["id"]
        == legacy_id
    )


def test_unknown_and_cross_project_sources_and_targets_are_rejected(client, episode):
    source = script(client, episode)
    url = path(episode, source)
    other = client.post("/api/v1/projects", json={"name": "另一个项目"}).json()
    outside = create(client, {"project_id": other["id"]}, "CHARACTER")
    prop = create(client, episode, "PROP")
    for target in [str(uuid4()), outside["id"], prop["id"]]:
        assert client.post(url, json=body(client, url, entry_id=target)).status_code == 422
    assert client.post(url, json=body(client, url, "未知角色")).status_code == 422
    assert client.get(path(episode, {"id": str(uuid4())})).status_code == 404
    assert client.get(path(episode, source, "story-analyses")).status_code == 404
    other_episode = client.post(
        f"/api/v1/projects/{other['id']}/episodes", json={"episode_no": 1, "title": "另集"}
    ).json()
    assert client.get(path(other_episode, source)).status_code == 404


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_permissions(client, role):
    owner = register(client, "matching-owner@example.com")
    project = client.post("/api/v1/projects", json={"name": "角色权限"}).json()
    episode = client.post(
        f"/api/v1/projects/{project['id']}/episodes", json={"episode_no": 1, "title": "第一集"}
    ).json()
    source = script(client, episode)
    url = path(episode, source)
    pending = body(client, url)
    other = register(client, "matching-other@example.com")
    if role != "outsider":
        client.post(
            f"/api/v1/projects/{project['id']}/members",
            headers={"Authorization": "Bearer " + owner["access_token"]},
            json={"email": other["user"]["email"], "role": role},
        )
    assert client.get(url).status_code == (404 if role == "outsider" else 200)
    assert client.post(url, json=pending).status_code == (
        200 if role == "editor" else 403 if role == "viewer" else 404
    )


def test_queued_generation_blocks_confirmation_but_waiting_review_allows_it(
    client, episode, settings
):
    run = start(client, episode).json()["run_id"]
    advance(client, settings, run)
    source = client.get(f"/api/v1/episodes/{episode['id']}/scripts").json()[0]
    url = path(episode, source)
    request = body(client, url)
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, UUID(run)).status = "QUEUED"
    assert client.get(url).json()["executing"] is True
    assert client.post(url, json=request).status_code == 409
    with client.app.state.session_factory.begin() as db:
        row = db.get(WorkflowRun, UUID(run))
        inputs = row.input_json
        row.status = "WAITING_REVIEW"
    response = client.post(url, json=request)
    assert response.status_code == 200, response.text
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, UUID(run)).input_json == inputs
        assert db.get(Script, UUID(source["id"])).status == "READY_FOR_REVIEW"


def test_archival_and_deletion_block_mutations_preserve_evidence(client, episode):
    from test_project_deletion import archive as archive_project
    from test_project_deletion import delete

    source = script(client, episode)
    url = path(episode, source)
    pending = body(client, url)
    project = client.get(f"/api/v1/projects/{episode['project_id']}").json()
    archive_project(client, project)
    assert client.get(url).status_code == 200
    assert client.post(url, json=pending).status_code == 409
    assert delete(client, project).status_code == 200
    assert client.get(url).status_code == 404
    assert client.post(url, json=pending).status_code == 404


def test_conflicting_precise_asset_versions_cannot_be_confirmed(client, episode):
    with client.app.state.session_factory.begin() as db:
        first = make_asset(db, episode["project_id"], "CHARACTER")
        second = make_asset(db, episode["project_id"], "CHARACTER")
    entry = create(client, episode, "CHARACTER", name="主角", asset=first)
    source = script(client, episode, character_assets=[{"character": "主角", "asset": second}])
    url = path(episode, source)
    result = client.post(url, json=body(client, url, entry_id=entry["id"]))
    assert result.json()["error"]["code"] == "CHARACTER_ASSET_CONFLICT"


@pytest.mark.legacy_schema
def test_confirmation_migration_refuses_loss_of_decisions(client, episode, migrated):
    source = script(client, episode)
    url = path(episode, source)
    assert client.post(url, json=body(client, url)).status_code == 200
    with pytest.raises(RuntimeError, match="Character identity data exists"):
        command.downgrade(migrated, "0009")
    assert client.get(url).json()["candidates"][0]["confirmation"]


@pytest.mark.legacy_schema
def test_alias_migration_refuses_loss_of_aliases(client, episode, migrated):
    create(client, episode, "CHARACTER", aliases=["昵称"])
    with pytest.raises(RuntimeError, match="Character identity data exists"):
        command.downgrade(migrated, "0009")


def test_confirmation_failure_rolls_back_identity_and_audit(client, episode, monkeypatch):
    from arclight.persistence.models import AuditEvent

    source = script(client, episode)
    url = path(episode, source)
    pending = body(client, url)
    monkeypatch.setattr("arclight.application.story_context.MAX_STORY_ENTRIES", 0)
    assert client.post(url, json=pending).status_code == 422
    assert client.get(entries_path(episode)).json() == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(CharacterConfirmation)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action.in_(["character.confirmed", "story_entry.created"]))
            )
            == 0
        )
    monkeypatch.setattr("arclight.application.story_context.MAX_STORY_ENTRIES", 100)
    assert client.post(url, json=pending).status_code == 200


def test_aliases_are_captured_in_writer_input_and_never_reinterpreted(client, episode):
    row = create(client, episode, "CHARACTER", name="阿青", aliases=["小青"])
    run_id = start(client, episode).json()["run_id"]
    assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    assert replace(client, episode, row, aliases=["青姐"]).status_code == 200
    with client.app.state.session_factory() as db:
        original = db.get(WorkflowRun, UUID(run_id)).input_json["story_context"]["entries"][0]
        assert original["aliases"] == ["小青"] and original["revision"] == 0


def test_postgres_duplicate_confirmation_creates_one_identity(client, episode, settings):
    require_postgres(settings)
    source = script(client, episode)
    url = path(episode, source)
    results = race(client.app, url, body(client, url), {"Authorization": "Bearer test-token"})
    assert [result.status_code for result in results] == [200, 200]
    assert results[0].json()["id"] == results[1].json()["id"]
    assert len(client.get(entries_path(episode)).json()) == 1


def test_postgres_different_sources_cannot_create_duplicate_identity(client, episode, settings):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi.testclient import TestClient

    from arclight.main import create_app

    require_postgres(settings)
    sources = [script(client, episode), script(client, episode)]
    urls = [path(episode, source) for source in sources]
    requests = [body(client, url) for url in urls]
    barrier = Barrier(2)

    def request(index):
        with TestClient(create_app(settings)) as connection:
            barrier.wait(timeout=15)
            return connection.post(
                urls[index], json=requests[index], headers={"Authorization": "Bearer test-token"}
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert sorted(result.status_code for result in results) == [200, 409]
    assert len(client.get(entries_path(episode)).json()) == 1
