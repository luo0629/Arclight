"""Writing targets, bounded distant evidence and frozen narrative provenance."""

import copy
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_series_planning import planned_outline as planned_outline
from test_story_ledger import approve_ledger, approved_script, ledger_content, save_ledger
from test_story_outline import confirm, save

from arclight.application.agent_memory_sources import capture_memory_sources
from arclight.application.context_sources import capture_sources, validate_sources
from arclight.application.writing_context import (
    load_narrative_projection,
    validate_narrative_context,
    validate_narrative_source,
)
from arclight.core.errors import DomainError
from arclight.persistence.models import DependencyManifest, Episode, Project, StoryEntry


@pytest.fixture
def writing_case(client, episode, planned_outline):
    old, characters, content = planned_outline
    saved = save(client, episode, old, content, "writing-approved-plan")
    assert saved.status_code == 201, saved.text
    approved = confirm(client, episode, saved.json())
    assert approved.status_code == 200, approved.text
    return {
        "episode": episode,
        "plan": approved.json(),
        "characters": characters,
        "request": {
            "series_plan_version_id": approved.json()["id"],
            "ledger_version_id": None,
            "expected_ledger_head_revision": 0,
            "target_story_time": 0,
            "target_scene_no": 1,
            "character_ids": [item["id"] for item in characters],
        },
    }


def preview(client, case, **changes):
    return client.post(
        f"/api/v1/episodes/{case['episode']['id']}/writing-context",
        json=case["request"] | changes,
    )


def ledger_baseline(client, case, *, content=None):
    script = approved_script(client, case["episode"])
    value = content or ledger_content(case["episode"], script, case["characters"][0]["id"])
    created = save_ledger(client, case["episode"], value)
    approved = approve_ledger(client, case["episode"], created)
    case["request"].update(
        ledger_version_id=created["version"]["id"],
        expected_ledger_head_revision=approved["head"]["revision"],
    )
    return script, created, approved


def test_capture_is_deterministic_read_only_and_never_promotes_plans(client, writing_case):
    case = writing_case
    with client.app.state.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(DependencyManifest))
    first = preview(client, case)
    assert first.status_code == 200, first.text
    second = preview(client, case)
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    value = first.json()
    assert value["context"]["ledger_version_id"] is None
    assert value["projection"]["target"]["label"] == "WRITING_TARGET"
    assert "script_version_id" not in value["projection"]["target"]
    assert value["projection"]["plan"]["kind"] == "PLANNED"
    assert value["projection"]["facts"] == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(DependencyManifest)) == before
        sources = capture_sources(
            db,
            UUID(case["episode"]["project_id"]),
            {"narrative_context": value["context"]},
            {"required": {"assets": [], "global": {}}},
        )
        validate_sources(db, UUID(case["episode"]["project_id"]), sources)
        memory = capture_memory_sources(db, UUID(case["episode"]["project_id"]), sources)
        assert {item["category"] for item in memory["memory_sources"]} == {"EVIDENCE"}


def test_empty_role_selection_freezes_the_exact_planning_characters(client, writing_case):
    result = preview(client, writing_case, character_ids=[])
    assert result.status_code == 200, result.text
    assert result.json()["context"]["character_ids"] == [
        item["id"] for item in writing_case["characters"]
    ]


def test_null_ledger_is_explicit_and_cannot_ignore_existing_approval(client, writing_case):
    body = copy.deepcopy(writing_case["request"])
    body.pop("ledger_version_id")
    response = client.post(
        f"/api/v1/episodes/{writing_case['episode']['id']}/writing-context", json=body
    )
    assert response.status_code == 422
    ledger_baseline(client, writing_case)
    response = preview(client, writing_case, ledger_version_id=None)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"


def test_character_facts_are_filtered_before_sources_counts_and_pagination(client, writing_case):
    ledger_baseline(client, writing_case)
    response = preview(client, writing_case)
    assert response.status_code == 200, response.text
    value = response.json()
    author = value["projection"]
    assert len(author["facts"]) == len(author["proposals"]) == len(author["planned"]) == 1
    first, second = value["character_projections"]
    assert len(first["facts"]) == 1
    assert second["facts"] == [] and second["total"] == 0
    for character in (first, second):
        assert character["plan"] is None
        assert character["planned"] == character["proposals"] == []
        assert character["episode_index"] == character["causal_summaries"] == []
        serialized = json.dumps(character, ensure_ascii=False)
        assert "未来将公开信中的秘密" not in serialized
        assert "红印可能来自旧友" not in serialized
        assert "known_by" not in serialized
    with client.app.state.session_factory() as db:
        project_id = UUID(writing_case["episode"]["project_id"])
        for projection in (author, first, second):
            validate_sources(db, project_id, projection["sources"])
            memory = capture_memory_sources(db, project_id, projection["sources"])
            assert {item["category"] for item in memory["memory_sources"]} == {"EVIDENCE"}


@pytest.mark.parametrize("changes", [{"story_time": 100}, {"scene_no": 2}])
def test_character_cannot_move_the_frozen_knowledge_anchor(client, writing_case, changes):
    context = preview(client, writing_case).json()["context"]
    with client.app.state.session_factory() as db:
        project_id = UUID(writing_case["episode"]["project_id"])
        actor = db.get(Project, project_id).owner_id
        with pytest.raises(DomainError, match="冻结"):
            load_narrative_projection(
                db,
                project_id,
                actor,
                context,
                character_id=UUID(writing_case["characters"][0]["id"]),
                **changes,
            )


def extended_plan(client, case, count):
    content = copy.deepcopy(case["plan"]["content"])
    while len(content["episodes"]) < count:
        number = len(content["episodes"]) + 1
        content["episodes"].append(content["episodes"][-1] | {"episode_no": number})
        content["series_plan"]["episode_progressions"].append(
            copy.deepcopy(content["series_plan"]["episode_progressions"][-1])
            | {"episode_no": number, "caused_by_episode_nos": [1]}
        )
    content["target_episode_count"] = count
    saved = save(client, case["episode"], case["plan"], content, f"writing-{count}-episodes")
    assert saved.status_code == 201, saved.text
    approved = confirm(client, case["episode"], saved.json())
    assert approved.status_code == 200, approved.text
    case["plan"] = approved.json()
    case["request"]["series_plan_version_id"] = approved.json()["id"]


def test_distant_causal_summary_uses_exact_approved_script_and_real_target(client, writing_case):
    case = writing_case
    extended_plan(client, case, 6)
    source_episode = case["episode"]
    script, _, _ = ledger_baseline(client, case)
    response = client.post(
        f"/api/v1/projects/{source_episode['project_id']}/episodes",
        json={"episode_no": 6, "title": "旧信的回应", "target_duration_ms": 30000},
    )
    assert response.status_code == 201, response.text
    case["episode"] = response.json()
    response = preview(client, case, target_story_time=50)
    assert response.status_code == 200, response.text
    projection = response.json()["projection"]
    assert projection["target"]["episode_no"] == 6
    assert projection["causal_summaries"][0]["episode_no"] == 1
    assert projection["causal_summaries"][0]["script_version_id"] == script["id"]
    assert projection["causal_summaries"][0]["source"]["approval_decision_id"]
    assert projection["related_next_offset"] is None
    assert projection["related_missing"] == []


def test_historical_query_proof_keeps_exact_script_after_newer_approval(client, writing_case):
    case = writing_case
    old, _, _ = ledger_baseline(client, case)
    response = preview(client, case)
    assert response.status_code == 200, response.text
    sources = response.json()["projection"]["sources"]
    newer = approved_script(client, case["episode"], expected_version_no=old["version_no"])
    assert newer["id"] != old["id"]
    with client.app.state.session_factory() as db:
        validate_sources(db, UUID(case["episode"]["project_id"]), sources)


def test_forged_context_proofs_and_foreign_roles_are_rejected(client, writing_case):
    response = preview(client, writing_case, character_ids=[str(uuid4())])
    assert response.status_code == 409
    context = preview(client, writing_case).json()["context"]
    with client.app.state.session_factory() as db:
        project_id = UUID(writing_case["episode"]["project_id"])
        episode = db.get(Episode, UUID(writing_case["episode"]["id"]))
        actor = db.get(Project, project_id).owner_id
        changed = copy.deepcopy(context)
        changed["plan_sha256"] = "0" * 64
        with pytest.raises(DomainError):
            validate_narrative_context(db, episode, actor, changed)
        source = copy.deepcopy(preview(client, writing_case).json()["projection"]["sources"][-1])
        source["result_sha256"] = "0" * 64
        with pytest.raises(DomainError):
            validate_narrative_source(db, project_id, source)


def test_large_season_is_bounded_and_exposes_independent_index_pagination(client, writing_case):
    extended_plan(client, writing_case, 1000)
    response = preview(client, writing_case)
    assert response.status_code == 200, response.text
    result = response.json()["projection"]
    assert result["index_total"] == 1000
    assert len(result["episode_index"]) <= 20
    assert result["index_next_offset"] == len(result["episode_index"])
    assert result["index_truncated"] is True
    assert "limits" in result["plan"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 65536


def test_frozen_history_reads_old_roles_but_new_approval_revalidates_them(client, writing_case):
    context = preview(client, writing_case).json()["context"]
    with client.app.state.session_factory() as db:
        character = db.get(StoryEntry, UUID(writing_case["characters"][0]["id"]))
        character.name = "改名后的角色"
        character.revision += 1
        db.commit()
    with client.app.state.session_factory() as db:
        project_id = UUID(writing_case["episode"]["project_id"])
        actor = db.get(Project, project_id).owner_id
        episode = db.get(Episode, UUID(writing_case["episode"]["id"]))
        result = load_narrative_projection(db, project_id, actor, context)
        assert result["plan"]["sha256"] == context["plan_sha256"]
        character_view = load_narrative_projection(
            db,
            project_id,
            actor,
            context,
            character_id=UUID(writing_case["characters"][0]["id"]),
        )
        assert character_view["character_name"] == writing_case["characters"][0]["name"]
        validate_sources(db, project_id, character_view["sources"])
        with pytest.raises(DomainError, match="人物"):
            validate_narrative_context(db, episode, actor, context, check_head=True)


@pytest.mark.parametrize("field,value", [("approved_by", "forged-approver"), ("version_no", 999)])
def test_historical_query_rejects_forged_script_authority(client, writing_case, field, value):
    ledger_baseline(client, writing_case)
    source = copy.deepcopy(preview(client, writing_case).json()["projection"]["sources"][-1])
    assert source["script_sources"]
    source["script_sources"][0][field] = value
    with client.app.state.session_factory() as db:
        project_id = UUID(writing_case["episode"]["project_id"])
        with pytest.raises(DomainError, match="精确批准来源"):
            validate_narrative_source(db, project_id, source)


def test_character_query_cannot_expand_the_frozen_character_scope(client, writing_case):
    context = preview(
        client, writing_case, character_ids=[writing_case["characters"][0]["id"]]
    ).json()["context"]
    with client.app.state.session_factory() as db:
        project_id = UUID(writing_case["episode"]["project_id"])
        actor = db.get(Project, project_id).owner_id
        with pytest.raises(DomainError, match="写作人物范围"):
            load_narrative_projection(
                db,
                project_id,
                actor,
                context,
                character_id=UUID(writing_case["characters"][1]["id"]),
            )
