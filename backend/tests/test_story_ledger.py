"""Real API and database coverage for the precise story ledger."""

import copy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from arclight.persistence.models import (
    AssetVersion,
    Project,
    StoryEntry,
    StoryLedgerApproval,
    StoryLedgerHead,
    StoryLedgerVersion,
)
from arclight.providers.text import MockTextProvider


def ledger_path(episode):
    return f"/api/v1/projects/{episode['project_id']}/story-ledger"


def approved_script(client, episode, *, expected_version_no=0):
    content = MockTextProvider().script("雨夜送信", {}).model_dump(mode="json")
    content["scenes"].append(
        {
            "scene_id": str(uuid4()),
            "scene_no": len(content["scenes"]) + 1,
            "title": "无对白的清晨",
            "action": "晨光照亮信封。",
            "dialogue": [],
        }
    )
    prefix = f"/api/v1/episodes/{episode['id']}/scripts"
    saved = client.post(
        prefix, json={"expected_version_no": expected_version_no, "content": content}
    )
    assert saved.status_code == 201, saved.text
    script = saved.json()
    approved = client.post(prefix + f"/{script['id']}/review", json={"decision": "APPROVE"})
    assert approved.status_code == 200, approved.text
    return script


def ledger_content(episode, script, character_id=None):
    scenes = script["content_json"]["scenes"]
    mappings = [
        {
            "id": str(uuid4()),
            "episode_id": episode["id"],
            "episode_no": episode["episode_no"],
            "script_version_id": script["id"],
            "scene_id": scene["scene_id"],
            "scene_no": scene["scene_no"],
            "segment_no": 1,
            "story_time": index * 10,
        }
        for index, scene in enumerate(scenes)
    ]
    entries = []
    for status, value in (
        ("CONFIRMED", "已经收到红信"),
        ("PROPOSED", "红印可能来自旧友"),
        ("AUTHOR_PLANNED", "未来将公开信中的秘密"),
    ):
        entry = {
            "id": str(uuid4()),
            "kind": "EVENT",
            "status": status,
            "subject_id": character_id,
            "target_id": None,
            "state_key": "letter-" + status.lower(),
            "value": value,
            "story_start": 0,
            "story_end": None,
            "disclosure_start": mappings[0]["id"],
            "disclosure_end": None,
            "known_by": (
                [{"character_id": character_id, "story_start": 0, "story_end": None}]
                if character_id
                else []
            ),
            "causes": [],
        }
        if status != "AUTHOR_PLANNED":
            entry["source_script_version_id"] = script["id"]
            entry["source_scene_id"] = scenes[0]["scene_id"]
        entries.append(entry)
    return {"schema_version": "1.0", "scene_times": mappings, "entries": entries}


def save_ledger(client, episode, content, *, revision=0, base_version_id=None, key="ledger-create"):
    response = client.post(
        ledger_path(episode) + "/versions",
        json={
            "expected_revision": revision,
            "base_version_id": base_version_id,
            "content": content,
        },
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def approve_ledger(client, episode, created, *, key="ledger-approve"):
    response = client.post(
        ledger_path(episode) + f"/versions/{created['version']['id']}/approve",
        json={"expected_revision": created["head"]["revision"]},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def ledger_case(client, episode):
    entries = []
    for kind, name in (("CHARACTER", "阿青"), ("CHARACTER", "石岚"), ("PROP", "红信")):
        response = client.post(
            f"/api/v1/projects/{episode['project_id']}/story-entries",
            json={"kind": kind, "name": name},
        )
        assert response.status_code == 201, response.text
        entries.append(response.json())
    script = approved_script(client, episode)
    content = ledger_content(episode, script, entries[0]["id"])
    created = save_ledger(client, episode, content)
    approved = approve_ledger(client, episode, created)
    return {
        "episode": episode,
        "script": script,
        "content": content,
        "character": entries[0],
        "other_character": entries[1],
        "prop": entries[2],
        "created": created,
        "approved": approved,
    }


def query_body(case, *, perspective="AUTHOR", **changes):
    first = case["content"]["scene_times"][0]
    query = {
        "episode_id": first["episode_id"],
        "script_version_id": first["script_version_id"],
        "scene_id": first["scene_id"],
        "segment_no": first["segment_no"],
        "perspective": perspective,
        **changes,
    }
    return {"version_id": case["created"]["version"]["id"], "query": query}


def test_sources_are_exact_paginated_and_keep_silent_scenes(client, ledger_case):
    case = ledger_case
    path = ledger_path(case["episode"])
    page = client.get(path + "/sources", params={"limit": 1, "offset": 0}).json()
    assert page["total"] == 1
    assert page["limit"] == 1 and page["offset"] == 0
    source = page["items"][0]
    assert source["script_version_id"] == case["script"]["id"]
    assert source["registry_version_id"] == case["script"]["id"]
    assert source["content_sha256"]
    assert source["scenes"][-1]["dialogue_count"] == 0
    assert (
        source["scenes"][-1]["scene_id"] == case["script"]["content_json"]["scenes"][-1]["scene_id"]
    )
    assert client.get(path + "/sources", params={"limit": 1, "offset": 1}).json()["items"] == []
    versions = client.get(path + "/versions", params={"limit": 1}).json()
    assert versions["total"] == 1
    summary = versions["items"][0]
    assert summary["id"] == case["created"]["version"]["id"]
    assert "content" not in summary and "source_manifest" not in summary
    detail = client.get(path + "/versions/" + summary["id"]).json()
    assert detail["content"] == case["created"]["version"]["content"]
    assert detail["approval"]["id"] == case["approved"]["approval"]["id"]
    assert detail["source_manifest"]["scripts"][0]["approval_decision_id"]
    assert detail["source_manifest"]["story_entries"][0]["revision"] == 0


def test_exact_query_isolates_plans_proposals_and_character_knowledge(client, ledger_case):
    case = ledger_case
    path = ledger_path(case["episode"]) + "/query"
    body = query_body(case)
    author = client.post(path, json=body)
    assert author.status_code == 200, author.text
    value = author.json()
    assert len(value["facts"]) == 1
    assert value["planned"] == value["proposals"] == []
    assert value["approval_id"] == case["approved"]["approval"]["id"]
    assert value["content_sha256"] == case["created"]["version"]["content_sha256"]
    assert value["source_manifest_sha256"] == case["created"]["version"]["source_manifest_sha256"]
    assert value["query_sha256"] and value["result_sha256"]
    assert "known_by" not in value["facts"][0]
    assert client.post(path, json=body).json() == value
    all_entries = client.post(
        path, json=query_body(case, include_planned=True, include_proposed=True)
    ).json()
    assert (
        len(all_entries["facts"])
        == len(all_entries["planned"])
        == len(all_entries["proposals"])
        == 1
    )
    assert all_entries["query_sha256"] != value["query_sha256"]
    known = client.post(
        path, json=query_body(case, perspective="CHARACTER", character_id=case["character"]["id"])
    ).json()
    assert len(known["facts"]) == 1 and known["planned"] == known["proposals"] == []
    unknown = client.post(
        path,
        json=query_body(case, perspective="CHARACTER", character_id=case["other_character"]["id"]),
    ).json()
    assert unknown["facts"] == unknown["planned"] == unknown["proposals"] == []
    assert (
        client.post(
            path,
            json=query_body(
                case,
                perspective="CHARACTER",
                character_id=case["character"]["id"],
                include_planned=True,
            ),
        ).status_code
        == 422
    )


def test_idempotency_replays_receipts_and_cas_keeps_history(client, ledger_case):
    case = ledger_case
    path = ledger_path(case["episode"])
    replay = save_ledger(client, case["episode"], case["content"])
    assert replay == case["created"]
    assert approve_ledger(client, case["episode"], case["created"]) == case["approved"]

    from sqlalchemy import func

    from arclight.persistence.models import AuditEvent

    with client.app.state.session_factory() as db:
        identity = UUID(case["created"]["version"]["id"])
        assert (
            db.scalar(
                select(func.count())
                .select_from(StoryLedgerVersion)
                .where(StoryLedgerVersion.project_id == UUID(case["episode"]["project_id"]))
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(StoryLedgerApproval)
                .where(StoryLedgerApproval.version_id == identity)
            )
            == 1
        )
        for action in ("story_ledger.version_created", "story_ledger.approved"):
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.resource_id == identity, AuditEvent.action == action)
                )
                == 1
            )
    changed = copy.deepcopy(case["content"])
    changed["entries"][0]["value"] = "另一份内容"
    conflict = client.post(
        path + "/versions",
        json={
            "expected_revision": 0,
            "base_version_id": None,
            "content": changed,
        },
        headers={"Idempotency-Key": "ledger-create"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    stale = client.post(
        path + "/versions",
        json={
            "expected_revision": 0,
            "base_version_id": None,
            "content": changed,
        },
        headers={"Idempotency-Key": "new-stale"},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
    wrong_base = client.post(
        path + "/versions",
        json={
            "expected_revision": 2,
            "base_version_id": None,
            "content": changed,
        },
        headers={"Idempotency-Key": "wrong-base"},
    )
    assert wrong_base.status_code == 409
    assert client.get(path).json() == case["approved"]["head"]
    replacement = save_ledger(
        client,
        case["episode"],
        changed,
        revision=2,
        base_version_id=case["created"]["version"]["id"],
        key="replacement",
    )
    assert replacement["version"]["version_no"] == 2
    assert replacement["head"]["revision"] == 3
    stale_approval = client.post(
        path + f"/versions/{case['created']['version']['id']}/approve",
        json={"expected_revision": 3},
        headers={"Idempotency-Key": "approve-old"},
    )
    assert stale_approval.status_code == 409
    page = client.get(path + "/versions", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 2 and page["items"][0]["id"] == case["created"]["version"]["id"]


def test_draft_confirmed_is_proposal_until_owner_approval_and_proof_changes(client, episode):
    script = approved_script(client, episode)
    content = ledger_content(episode, script)
    created = save_ledger(client, episode, content)
    case = {"episode": episode, "content": content, "created": created}
    body = query_body(case, include_proposed=True)
    path = ledger_path(episode) + "/query"
    before = client.post(path, json=body).json()
    assert before["approval_id"] is None and before["facts"] == []
    assert len(before["proposals"]) == 2
    assert all(item["status"] == "PROPOSED" for item in before["proposals"])
    approved = approve_ledger(client, episode, created)
    after = client.post(path, json=body).json()
    assert len(after["facts"]) == 1 and len(after["proposals"]) == 1
    assert after["approval_id"] == approved["approval"]["id"]
    assert after["query_sha256"] != before["query_sha256"]


@pytest.mark.parametrize(
    "change", ["episode_no", "scene_no", "scene_id", "source_id", "entity_kind"]
)
def test_bad_source_and_identity_are_atomic(client, ledger_case, change):
    case = ledger_case
    content = copy.deepcopy(case["content"])
    if change == "episode_no":
        for mapping in content["scene_times"]:
            if mapping["episode_id"] == case["episode"]["id"]:
                mapping["episode_no"] += 100
    elif change == "scene_no":
        content["scene_times"][0]["scene_no"] += 100
    elif change == "scene_id":
        content["scene_times"][0]["scene_id"] = str(uuid4())
        for entry in content["entries"][:2]:
            entry["source_scene_id"] = content["scene_times"][0]["scene_id"]
    elif change == "source_id":
        foreign = str(uuid4())
        for mapping in content["scene_times"]:
            mapping["script_version_id"] = foreign
        for entry in content["entries"][:2]:
            entry["source_script_version_id"] = foreign
    else:
        content["entries"][0]["known_by"][0]["character_id"] = case["prop"]["id"]
    response = client.post(
        ledger_path(case["episode"]) + "/versions",
        json={
            "expected_revision": 2,
            "base_version_id": case["created"]["version"]["id"],
            "content": content,
        },
        headers={"Idempotency-Key": "invalid-" + change},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "STORY_LEDGER_SOURCE_INVALID"
    assert client.get(ledger_path(case["episode"])).json() == case["approved"]["head"]


@pytest.mark.parametrize("change", ["script_stale", "entity_revision", "episode_archived"])
def test_approval_revalidates_sources_and_preserves_unapproved_draft(client, episode, change):
    character = client.post(
        f"/api/v1/projects/{episode['project_id']}/story-entries",
        json={"kind": "CHARACTER", "name": "来源人物"},
    ).json()
    script = approved_script(client, episode)
    content = ledger_content(episode, script, character["id"])
    created = save_ledger(client, episode, content)
    with client.app.state.session_factory.begin() as db:
        if change == "script_stale":
            registry = db.get(AssetVersion, UUID(script["id"]))
            registry.stale = True
            registry.revision += 1
        elif change == "entity_revision":
            entry = db.get(StoryEntry, UUID(character["id"]))
            entry.name = "后来的名字"
            entry.revision += 1
        else:
            from arclight.persistence.models import Episode

            db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
    response = client.post(
        ledger_path(episode) + f"/versions/{created['version']['id']}/approve",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": "invalid-approval"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "STORY_LEDGER_SOURCE_INVALID"
    detail = client.get(ledger_path(episode) + "/versions/" + created["version"]["id"]).json()
    assert detail["approval"] is None
    assert client.get(ledger_path(episode)).json() == created["head"]
    if change in {"script_stale", "episode_archived"}:
        assert client.get(ledger_path(episode) + "/sources").json()["items"] == []


def test_approved_history_uses_frozen_identity_after_entity_change(client, ledger_case):
    case = ledger_case
    body = query_body(case, perspective="CHARACTER", character_id=case["character"]["id"])
    path = ledger_path(case["episode"]) + "/query"
    before = client.post(path, json=body).json()
    with client.app.state.session_factory.begin() as db:
        row = db.get(StoryEntry, UUID(case["character"]["id"]))
        row.kind = "LOCATION"
        row.archived = True
        row.name = "已归档的不同类型"
        row.revision += 1
        registry = db.get(AssetVersion, UUID(case["script"]["id"]))
        registry.stale = True
        registry.revision += 1
        from arclight.persistence.models import Episode

        db.get(Episode, UUID(case["episode"]["id"])).status = "ARCHIVED"
    after = client.post(path, json=body)
    assert after.status_code == 200, after.text
    assert after.json() == before


def test_query_rejects_forged_time_scope_and_foreign_character(client, ledger_case):
    case = ledger_case
    path = ledger_path(case["episode"]) + "/query"
    body = query_body(case)
    body["query"]["story_time"] = 999999
    assert client.post(path, json=body).status_code == 422
    for key in ("episode_id", "script_version_id", "scene_id"):
        body = query_body(case)
        body["query"][key] = str(uuid4())
        assert client.post(path, json=body).status_code == 422
    unknown = query_body(case, perspective="CHARACTER", character_id=str(uuid4()))
    assert client.post(path, json=unknown).status_code == 422
    impossible_role = query_body(case, perspective="CHARACTER", character_id=case["prop"]["id"])
    assert client.post(path, json=impossible_role).status_code == 422


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_member_permissions_and_owner_only_approval(client, role):
    from test_collaboration import create_episode, create_project, register

    owner = register(client, "ledger-owner@example.com")
    project = create_project(client)
    episode = create_episode(client, project)
    script = approved_script(client, episode)
    content = ledger_content(episode, script)
    created = save_ledger(client, episode, content)
    other = register(client, "ledger-other@example.com")
    if role != "outsider":
        response = client.post(
            f"/api/v1/projects/{project['id']}/members",
            headers={"Authorization": "Bearer " + owner["access_token"]},
            json={"email": other["user"]["email"], "role": role},
        )
        assert response.status_code == 201, response.text
    assert client.get(ledger_path(episode)).status_code == (404 if role == "outsider" else 200)
    saved = client.post(
        ledger_path(episode) + "/versions",
        json={
            "expected_revision": 1,
            "base_version_id": created["version"]["id"],
            "content": content,
        },
        headers={"Idempotency-Key": "member-create"},
    )
    assert saved.status_code == (201 if role == "editor" else 403 if role == "viewer" else 404)
    response = client.post(
        ledger_path(episode) + f"/versions/{created['version']['id']}/approve",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": "member-approve"},
    )
    assert response.status_code == (404 if role == "outsider" else 403)


def test_archived_project_blocks_writes_and_requires_idempotency_header(client, ledger_case):
    case = ledger_case
    path = ledger_path(case["episode"])
    payload = {
        "expected_revision": 2,
        "base_version_id": case["created"]["version"]["id"],
        "content": case["content"],
    }
    assert client.post(path + "/versions", json=payload).status_code == 422
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(case["episode"]["project_id"])).archived = True
    response = client.post(
        path + "/versions", json=payload, headers={"Idempotency-Key": "archived"}
    )
    assert response.status_code == 409
    assert client.get(path).status_code == 200


def postgres_only(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locking")


def test_postgres_concurrent_creates_have_one_cas_winner(client, ledger_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from arclight.application.story_ledger import create_version
    from arclight.core.errors import DomainError
    from arclight.domain.story_ledger import StoryLedgerContent

    postgres_only(client)
    case = ledger_case
    factory = client.app.state.session_factory
    project_id = UUID(case["episode"]["project_id"])
    with factory() as db:
        actor = db.get(Project, project_id).owner_id
    ready = Barrier(2)

    def create(index):
        ready.wait(timeout=5)
        try:
            with factory.begin() as db:
                result = create_version(
                    db,
                    project_id,
                    actor,
                    2,
                    UUID(case["created"]["version"]["id"]),
                    StoryLedgerContent.model_validate(case["content"]),
                    "race-" + str(index),
                )
                return result["version"]["id"]
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (1, 2)))
    assert results.count("VERSION_CONFLICT") == 1
    with factory() as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(StoryLedgerVersion).where(
                            StoryLedgerVersion.project_id == project_id
                        )
                    )
                )
            )
            == 2
        )
        assert (
            db.scalar(
                select(StoryLedgerHead).where(StoryLedgerHead.project_id == project_id)
            ).revision
            == 3
        )


def test_postgres_approval_waits_for_source_lock_then_revalidates(client, ledger_case):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from arclight.application.story_ledger import approve_version
    from arclight.core.errors import DomainError

    postgres_only(client)
    case = ledger_case
    created = save_ledger(
        client,
        case["episode"],
        case["content"],
        revision=2,
        base_version_id=case["created"]["version"]["id"],
        key="source-race-draft",
    )
    factory = client.app.state.session_factory
    project_id = UUID(case["episode"]["project_id"])
    started = Event()

    def approve(actor):
        started.set()
        try:
            with factory.begin() as db:
                approve_version(
                    db, project_id, UUID(created["version"]["id"]), actor, 3, "source-race-approve"
                )
            return "APPROVED"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as holder:
            project = holder.scalar(
                select(Project).where(Project.id == project_id).with_for_update()
            )
            future = pool.submit(approve, project.owner_id)
            assert started.wait(timeout=5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.15)
            registry = holder.get(AssetVersion, UUID(case["script"]["id"]))
            registry.stale = True
            registry.revision += 1
        assert future.result(timeout=5) == "STORY_LEDGER_SOURCE_INVALID"
    with factory() as db:
        assert (
            db.scalar(
                select(StoryLedgerApproval).where(
                    StoryLedgerApproval.version_id == UUID(created["version"]["id"])
                )
            )
            is None
        )
        assert (
            db.scalar(
                select(StoryLedgerHead).where(StoryLedgerHead.project_id == project_id)
            ).revision
            == 3
        )


def test_postgres_approval_rechecks_owner_after_project_lock(client, ledger_case):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from arclight.application.story_ledger import approve_version
    from arclight.core.errors import DomainError

    postgres_only(client)
    case = ledger_case
    created = save_ledger(
        client,
        case["episode"],
        case["content"],
        revision=2,
        base_version_id=case["created"]["version"]["id"],
        key="owner-race-draft",
    )
    factory = client.app.state.session_factory
    project_id = UUID(case["episode"]["project_id"])
    started = Event()

    def approve(actor):
        started.set()
        try:
            with factory.begin() as db:
                approve_version(
                    db, project_id, UUID(created["version"]["id"]), actor, 3, "owner-race-approve"
                )
            return "APPROVED"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as holder:
            project = holder.scalar(
                select(Project).where(Project.id == project_id).with_for_update()
            )
            future = pool.submit(approve, project.owner_id)
            assert started.wait(timeout=5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.15)
            project.owner_id = "replacement-owner"
        assert future.result(timeout=5) == "NOT_FOUND"
    with factory() as db:
        assert (
            db.scalar(
                select(StoryLedgerApproval).where(
                    StoryLedgerApproval.version_id == UUID(created["version"]["id"])
                )
            )
            is None
        )
        assert (
            db.scalar(
                select(StoryLedgerHead).where(StoryLedgerHead.project_id == project_id)
            ).revision
            == 3
        )


def test_existing_cross_project_sources_roles_and_versions_are_rejected(client, ledger_case):
    from test_collaboration import create_episode, create_project

    case = ledger_case
    other_project = create_project(client)
    other_episode = create_episode(client, other_project)
    other_script = approved_script(client, other_episode)
    role = client.post(
        f"/api/v1/projects/{other_project['id']}/story-entries",
        json={"kind": "CHARACTER", "name": "其他项目的真实人物"},
    )
    assert role.status_code == 201, role.text
    other_role = role.json()
    other_content = ledger_content(other_episode, other_script, other_role["id"])
    other_created = save_ledger(client, other_episode, other_content, key="other-ledger")
    path = ledger_path(case["episode"])
    other_version_id = other_created["version"]["id"]
    assert client.get(path + "/versions/" + other_version_id).status_code == 404
    assert (
        client.post(
            path + "/query", json={**query_body(case), "version_id": other_version_id}
        ).status_code
        == 404
    )
    assert (
        client.post(
            path + f"/versions/{other_version_id}/approve",
            json={"expected_revision": 2},
            headers={"Idempotency-Key": "foreign-approve"},
        ).status_code
        == 404
    )
    for index, content in enumerate((other_content, copy.deepcopy(case["content"]))):
        if index == 1:
            content["entries"][0]["known_by"][0]["character_id"] = other_role["id"]
        response = client.post(
            path + "/versions",
            json={
                "expected_revision": 2,
                "base_version_id": case["created"]["version"]["id"],
                "content": content,
            },
            headers={"Idempotency-Key": "foreign-source-" + str(index)},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "STORY_LEDGER_SOURCE_INVALID"
    assert (
        client.post(
            path + "/query",
            json=query_body(case, perspective="CHARACTER", character_id=other_role["id"]),
        ).status_code
        == 422
    )
    foreign_target = other_content["scene_times"][0]
    body = query_body(case)
    for name in ("episode_id", "script_version_id", "scene_id", "segment_no"):
        body["query"][name] = foreign_target[name]
    assert client.post(path + "/query", json=body).status_code == 422
    assert client.get(path).json() == case["approved"]["head"]
    assert client.get(ledger_path(other_episode)).json() == other_created["head"]


def test_revoked_editor_cannot_read_write_replay_or_approve(client):
    from test_collaboration import create_episode, create_project, register

    owner = register(client, "ledger-revoke-owner@example.com")
    project = create_project(client)
    episode = create_episode(client, project)
    script = approved_script(client, episode)
    content = ledger_content(episode, script)
    created = save_ledger(client, episode, content)
    approved = approve_ledger(client, episode, created)
    member = register(client, "ledger-revoke-editor@example.com")
    owner_headers = {"Authorization": "Bearer " + owner["access_token"]}
    member_path = f"/api/v1/projects/{project['id']}/members"
    granted = client.post(
        member_path,
        headers=owner_headers,
        json={"email": member["user"]["email"], "role": "editor"},
    )
    assert granted.status_code == 201, granted.text
    path = ledger_path(episode)
    assert client.get(path).status_code == 200
    member_created = save_ledger(
        client,
        episode,
        content,
        revision=approved["head"]["revision"],
        base_version_id=created["version"]["id"],
        key="editor-before-revoke",
    )
    assert (
        client.delete(member_path + "/" + member["user"]["id"], headers=owner_headers).status_code
        == 204
    )
    for suffix in ("", "/sources", "/versions", "/versions/" + member_created["version"]["id"]):
        assert client.get(path + suffix).status_code == 404
    case = {"episode": episode, "content": content, "created": member_created}
    assert client.post(path + "/query", json=query_body(case)).status_code == 404
    for revision, base, key in (
        (2, created["version"]["id"], "editor-before-revoke"),
        (3, member_created["version"]["id"], "editor-after-revoke"),
    ):
        denied = client.post(
            path + "/versions",
            json={"expected_revision": revision, "base_version_id": base, "content": content},
            headers={"Idempotency-Key": key},
        )
        assert denied.status_code == 404, denied.text
    denied = client.post(
        path + f"/versions/{member_created['version']['id']}/approve",
        json={"expected_revision": 3},
        headers={"Idempotency-Key": "revoked-approve"},
    )
    assert denied.status_code == 404, denied.text
    assert client.get(path, headers=owner_headers).json() == member_created["head"]


def test_approval_rejects_simulated_corrupt_registry_checksum_atomically(
    client, ledger_case, monkeypatch
):
    """Proxy one registry read; its real immutable checksum and guards stay intact."""
    from sqlalchemy.orm import Session

    case = ledger_case
    draft = save_ledger(
        client,
        case["episode"],
        case["content"],
        revision=2,
        base_version_id=case["created"]["version"]["id"],
        key="checksum-draft",
    )
    script_id = UUID(case["script"]["id"])
    real_get = Session.get
    reads = []

    class CorruptRegistry:
        def __init__(self, row):
            self.row = row

        @property
        def checksum(self):
            return "0" * 64

        def __getattr__(self, name):
            return getattr(self.row, name)

    def corrupt_get(session, entity, identity, *args, **kwargs):
        row = real_get(session, entity, identity, *args, **kwargs)
        if entity is AssetVersion and identity == script_id and row is not None:
            reads.append(identity)
            return CorruptRegistry(row)
        return row

    with monkeypatch.context() as patch:
        patch.setattr(Session, "get", corrupt_get)
        response = client.post(
            ledger_path(case["episode"]) + f"/versions/{draft['version']['id']}/approve",
            json={"expected_revision": 3},
            headers={"Idempotency-Key": "corrupt-approval"},
        )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "STORY_LEDGER_SOURCE_INVALID"
    assert reads == [script_id]
    assert client.get(ledger_path(case["episode"])).json() == draft["head"]
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(StoryLedgerApproval).where(
                    StoryLedgerApproval.version_id == UUID(draft["version"]["id"])
                )
            )
            is None
        )
        registry = db.get(AssetVersion, script_id)
        assert (
            registry.checksum
            == case["created"]["version"]["source_manifest"]["scripts"][0]["registry_checksum"]
        )
