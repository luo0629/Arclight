"""Keep explicit ledger consumption separate from legacy static story memory."""

import json
from copy import deepcopy
from uuid import UUID

from test_story_ledger import approve_ledger, save_ledger
from test_story_ledger import ledger_case as ledger_case
from test_story_source_revisions import save_text

from arclight.application.story_context import build_story_context


def query_version(client, case, version_id):
    mapping = case["content"]["scene_times"][0]
    response = client.post(
        f"/api/v1/projects/{case['episode']['project_id']}/story-ledger/query",
        json={
            "version_id": version_id,
            "query": {
                "episode_id": mapping["episode_id"],
                "script_version_id": mapping["script_version_id"],
                "scene_id": mapping["scene_id"],
                "segment_no": mapping.get("segment_no", 1),
                "perspective": "AUTHOR",
                "include_planned": True,
                "include_proposed": True,
            },
            "limit": 100,
            "offset": 0,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_ledger_does_not_enter_legacy_static_context_or_frozen_memory(client, ledger_case):
    case = ledger_case
    episode = case["episode"]
    with client.app.state.session_factory.begin() as db:
        static_context = build_story_context(db, UUID(episode["project_id"]))
    assert static_context["schema_version"] == 1
    static_ids = {entry["id"] for entry in static_context["entries"]}
    ledger_ids = {entry["id"] for entry in case["content"]["entries"]}
    ledger_ids.add(case["created"]["version"]["id"])
    assert ledger_ids.isdisjoint(static_ids)
    assert not any(identity in json.dumps(static_context) for identity in ledger_ids)

    source = save_text(client, episode)
    started = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        headers={"Idempotency-Key": "ledger-legacy-memory"},
        json={"source_id": source["id"]},
    )
    assert started.status_code == 202, started.text
    cancelled = client.post(f"/api/v1/runs/{started.json()['run_id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    memory_path = f"/api/v1/runs/{started.json()['run_id']}/contexts/script/memory"
    initial = client.get(memory_path)
    assert initial.status_code == 200, initial.text
    initial_memory = initial.json()
    assert static_ids <= {item["source_id"] for item in initial_memory["memory"]["domain_facts"]}
    assert not any(identity in json.dumps(initial_memory) for identity in ledger_ids)

    updated = deepcopy(case["content"])
    planned = next(item for item in updated["entries"] if item["status"] == "AUTHOR_PLANNED")
    planned["value"] = "新增规划仍不能自动成为旧运行事实"
    created = save_ledger(
        client,
        episode,
        updated,
        revision=case["approved"]["head"]["revision"],
        base_version_id=case["created"]["version"]["id"],
        key="ledger-compat-next",
    )
    approve_ledger(client, episode, created, key="ledger-compat-next-approve")
    after = client.get(memory_path)
    assert after.status_code == 200, after.text
    assert after.json() == initial_memory
    with client.app.state.session_factory.begin() as db:
        assert build_story_context(db, UUID(episode["project_id"])) == static_context


def test_replacing_approved_head_preserves_old_exact_query_and_sources(client, ledger_case):
    case = ledger_case
    old_id = case["created"]["version"]["id"]
    old_query = query_version(client, case, old_id)
    updated = deepcopy(case["content"])
    planned = next(item for item in updated["entries"] if item["status"] == "AUTHOR_PLANNED")
    planned["value"] = "下一版作者规划"
    created = save_ledger(
        client,
        case["episode"],
        updated,
        revision=case["approved"]["head"]["revision"],
        base_version_id=old_id,
        key="ledger-exact-next",
    )
    approved = approve_ledger(client, case["episode"], created, key="ledger-exact-next-approve")
    assert approved["head"]["approved_version_id"] == created["version"]["id"]
    assert query_version(client, case, old_id) == old_query
