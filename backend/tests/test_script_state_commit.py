"""The real text review transaction is the only authority for screenplay state changes."""

import copy
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_collaboration import register
from test_series_planning import planned_outline  # noqa: F401
from test_writing_context import writing_case as writing_case

from arclight.agents.contracts import (
    Scene,
    ScriptContent,
    ScriptContentV3,
    legacy_script_view,
    read_script_content,
)
from arclight.application.script_state_commit import commit_script_state
from arclight.domain.story_ledger import StoryLedgerContent, read_ledger_content
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import (
    AssetVersion,
    Project,
    Script,
    StoryEntry,
    StoryLedgerApproval,
    StoryLedgerHead,
    StoryLedgerVersion,
    WorkflowRun,
)


def capture(client, case, **changes):
    request = copy.deepcopy(case["request"]) | changes
    response = client.post(
        f"/api/v1/episodes/{case['episode']['id']}/writing-context", json=request
    )
    assert response.status_code == 200, response.text
    return response.json()["context"]


def screenplay(case, context):
    characters = case["characters"]
    line_id = str(uuid4())
    return {
        "schema_version": "3.0",
        "title": "渡口送信",
        "summary": "阿青在渡口接过红信，石岚仍不知道信中秘密。",
        "estimated_duration_ms": 30000,
        "characters": [item["name"] for item in characters],
        "character_assets": [],
        "narrative_context": context,
        "scenes": [
            {
                "scene_id": str(uuid4()),
                "scene_no": 1,
                "title": "雨夜渡口",
                "action": "阿青护住红信，向石岚伸出空着的手。",
                "dialogue": [
                    {
                        "line_id": line_id,
                        "speaker": characters[0]["name"],
                        "text": "先过河，我会向你解释。",
                        "line_type": "dialogue",
                        "emotion": "谨慎而坚定",
                    }
                ],
                "dramatic_beat": {
                    "story_time": context["target_story_time"],
                    "character_objectives": [
                        {
                            "character_id": item["id"],
                            "purpose": "护送红信" if index == 0 else "辨认信的来历",
                            "emotion_start": "戒备",
                            "emotion_end": "愿意暂时合作",
                        }
                        for index, item in enumerate(characters)
                    ],
                    "conflict": "阿青需要协助过河，石岚却要求先看信。",
                    "dialogue_interactions": [
                        {
                            "line_ids": [line_id],
                            "character_ids": [item["id"] for item in characters],
                            "action": "阿青伸手示意同行，石岚放下阻拦的手臂。",
                        }
                    ],
                },
                "state_changes": [
                    {
                        "id": str(uuid4()),
                        "kind": "EVENT",
                        "state_key": "received-letter",
                        "value": "阿青接过红信",
                        "story_start": context["target_story_time"],
                        "known_by": [
                            {
                                "character_id": characters[0]["id"],
                                "story_start": context["target_story_time"],
                            }
                        ],
                        "causes": [],
                    }
                ],
            }
        ],
    }


def save(client, case, content, expected=0, base=None):
    response = client.post(
        f"/api/v1/episodes/{case['episode']['id']}/scripts",
        json={"expected_version_no": expected, "base_version_id": base, "content": content},
    )
    assert response.status_code == 201, response.text
    return response.json()


def review(client, case, script, decision="APPROVE"):
    prefix = f"/api/v1/episodes/{case['episode']['id']}/scripts/{script['id']}"
    impact = client.get(prefix + "/dependency-impact")
    assert impact.status_code == 200, impact.text
    payload = {"decision": decision, "impact_digest": impact.json().get("impact_digest")}
    return client.post(prefix + "/review", json=payload)


def test_manual_review_atomically_commits_exact_script_and_state(client, writing_case):
    case = writing_case
    context = capture(client, case)
    content = screenplay(case, context)
    saved = save(client, case, content)
    factory = client.app.state.session_factory
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 0
    result = review(client, case, saved)
    assert result.status_code == 200, result.text
    receipt = result.json()["story_state_receipt"]
    assert receipt["script_version_id"] == saved["id"]
    assert receipt["script_content_sha256"] == digest(saved["content_json"])
    assert receipt["revision"] == 1
    with factory() as db:
        ledger = db.get(StoryLedgerVersion, UUID(receipt["approved_version_id"]))
        approval = db.get(StoryLedgerApproval, UUID(receipt["approval_id"]))
        assert ledger.content_sha256 == approval.content_sha256
        parsed = read_ledger_content(ledger.content_json)
        assert parsed.schema_version == "2.0"
        entry = parsed.entries[0]
        assert entry.status == "CONFIRMED"
        assert str(entry.source_script_version_id) == saved["id"]
        assert str(entry.source_scene_id) == content["scenes"][0]["scene_id"]
        source = ledger.source_manifest_json["scripts"][0]
        assert source["approval_decision_id"] == receipt["approval_decision_id"]
        actor = db.get(Project, UUID(case["episode"]["project_id"])).owner_id
        assert commit_script_state(db, db.get(Script, UUID(saved["id"])), actor) == receipt
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 1


def test_reject_does_not_commit_any_state(client, writing_case):
    saved = save(client, writing_case, screenplay(writing_case, capture(client, writing_case)))
    result = review(client, writing_case, saved, "REJECT")
    assert result.status_code == 200, result.text
    assert "story_state_receipt" not in result.json()
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 0


@pytest.mark.parametrize("role,expected", [("editor", 403), ("viewer", 403), ("outsider", 404)])
def test_only_owner_can_approve_v3_and_mint_story_facts(client, writing_case, role, expected):
    case = writing_case
    saved = save(client, case, screenplay(case, capture(client, case)))
    owner_header = client.headers["Authorization"]
    member = register(client, f"{role}@series03.example.com")
    if role != "outsider":
        response = client.post(
            f"/api/v1/projects/{case['episode']['project_id']}/members",
            headers={"Authorization": owner_header},
            json={"email": member["user"]["email"], "role": role},
        )
        assert response.status_code == 201, response.text
    preview = client.post(
        f"/api/v1/episodes/{case['episode']['id']}/writing-context",
        json=case["request"],
    )
    assert preview.status_code == (404 if role == "outsider" else 200), preview.text
    factory = client.app.state.session_factory
    models = (StoryLedgerVersion, StoryLedgerApproval, HumanReviewDecision)
    with factory() as db:
        before = [db.scalar(select(func.count()).select_from(model)) for model in models]
    result = client.post(
        f"/api/v1/episodes/{case['episode']['id']}/scripts/{saved['id']}/review",
        json={"decision": "APPROVE"},
    )
    assert result.status_code == expected, result.text
    with factory() as db:
        assert db.get(Script, UUID(saved["id"])).status == "READY_FOR_REVIEW"
        assert db.get(AssetVersion, UUID(saved["id"])).status == "READY_FOR_REVIEW"
        assert [db.scalar(select(func.count()).select_from(model)) for model in models] == before


def test_archived_narrative_character_rolls_back_exact_approval(client, writing_case):
    case = writing_case
    saved = save(client, case, screenplay(case, capture(client, case)))
    factory = client.app.state.session_factory
    with factory() as db:
        db.get(StoryEntry, UUID(case["characters"][0]["id"])).archived = True
        db.commit()
        decisions_before = db.scalar(select(func.count()).select_from(HumanReviewDecision))
    result = review(client, case, saved)
    assert result.status_code == 422, result.text
    assert result.json()["error"]["code"] == "NARRATIVE_CHARACTER_INVALID"
    with factory() as db:
        assert db.get(Script, UUID(saved["id"])).status == "READY_FOR_REVIEW"
        assert db.get(AssetVersion, UUID(saved["id"])).status == "READY_FOR_REVIEW"
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 0
        assert db.scalar(select(func.count()).select_from(StoryLedgerApproval)) == 0
        assert db.scalar(select(func.count()).select_from(HumanReviewDecision)) == decisions_before


def test_competing_drafts_conflict_rolls_back_decision_registry_and_script(client, writing_case):
    case = writing_case
    context = capture(client, case)
    first = save(client, case, screenplay(case, context))
    second = save(client, case, screenplay(case, context), expected=1, base=first["id"])
    accepted = review(client, case, first)
    assert accepted.status_code == 200, accepted.text
    rejected = review(client, case, second)
    assert rejected.status_code == 409, rejected.text
    with client.app.state.session_factory() as db:
        assert db.get(Script, UUID(second["id"])).status == "READY_FOR_REVIEW"
        assert db.get(AssetVersion, UUID(second["id"])).status == "READY_FOR_REVIEW"
        request = db.scalar(
            select(HumanReviewRequest).where(
                HumanReviewRequest.creation_key == "text:" + second["id"]
            )
        )
        assert (
            db.scalar(
                select(HumanReviewDecision).where(HumanReviewDecision.request_id == request.id)
            )
            is None
        )
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 1
        assert db.scalar(select(StoryLedgerHead)).revision == 1


def test_explicit_same_time_replacement_keeps_old_source_and_ledger_history(client, writing_case):
    case = writing_case
    first = save(client, case, screenplay(case, capture(client, case)))
    accepted = review(client, case, first)
    assert accepted.status_code == 200, accepted.text
    receipt = accepted.json()["story_state_receipt"]
    original_id = UUID(receipt["approved_version_id"])
    with client.app.state.session_factory() as db:
        original = copy.deepcopy(db.get(StoryLedgerVersion, original_id).content_json)
    context = capture(
        client, case, ledger_version_id=str(original_id), expected_ledger_head_revision=1
    )
    revised = copy.deepcopy(first["content_json"])
    revised["narrative_context"] = context
    proposal = revised["scenes"][0]["state_changes"][0]
    proposal["supersedes_entry_id"] = proposal["id"]
    proposal["value"] = "阿青拒绝接信，将红信交还石岚"
    revised["scenes"][0]["action"] = "阿青把红信放回石岚手中。"
    second = save(client, case, revised, expected=1, base=first["id"])
    result = review(client, case, second)
    assert result.status_code == 200, result.text
    with client.app.state.session_factory() as db:
        assert db.get(StoryLedgerVersion, original_id).content_json == original
        ledger = db.get(
            StoryLedgerVersion, UUID(result.json()["story_state_receipt"]["approved_version_id"])
        )
        parsed = read_ledger_content(ledger.content_json)
        assert len(parsed.scene_times) == 2
        assert {str(item.script_version_id) for item in parsed.scene_times} == {
            first["id"],
            second["id"],
        }
        assert len(parsed.entries) == 1
        assert str(parsed.entries[0].source_script_version_id) == second["id"]
        assert parsed.entries[0].value == proposal["value"]
        assert db.get(Script, UUID(first["id"])).content_json == first["content_json"]


def test_unchanged_proposals_in_a_new_draft_keep_the_original_fact_source(client, writing_case):
    case = writing_case
    first = save(client, case, screenplay(case, capture(client, case)))
    accepted = review(client, case, first)
    assert accepted.status_code == 200, accepted.text
    receipt = accepted.json()["story_state_receipt"]
    context = capture(
        client,
        case,
        ledger_version_id=receipt["approved_version_id"],
        expected_ledger_head_revision=1,
    )
    revised = copy.deepcopy(first["content_json"])
    revised["narrative_context"] = context
    revised["summary"] = "调整叙事摘要，已确认接信事实保持不变。"
    second = save(client, case, revised, expected=1, base=first["id"])
    result = review(client, case, second)
    assert result.status_code == 200, result.text
    with client.app.state.session_factory() as db:
        ledger = db.get(
            StoryLedgerVersion, UUID(result.json()["story_state_receipt"]["approved_version_id"])
        )
        parsed = read_ledger_content(ledger.content_json)
        assert len(parsed.entries) == 1
        assert str(parsed.entries[0].source_script_version_id) == first["id"]
        assert len(parsed.scene_times) == 2


@pytest.mark.parametrize("forgery", ["status", "source_script_version_id", "source_scene_id"])
def test_model_cannot_submit_approval_authority_or_invent_source(client, writing_case, forgery):
    content = screenplay(writing_case, capture(client, writing_case))
    content["scenes"][0]["state_changes"][0][forgery] = (
        "CONFIRMED" if forgery == "status" else str(uuid4())
    )
    response = client.post(
        f"/api/v1/episodes/{writing_case['episode']['id']}/scripts",
        json={"expected_version_no": 0, "content": content},
    )
    assert response.status_code == 422, response.text


def test_unknown_state_entity_rolls_back_approval(client, writing_case):
    content = screenplay(writing_case, capture(client, writing_case))
    proposal = content["scenes"][0]["state_changes"][0]
    proposal.update(kind="INJURY", subject_id=str(uuid4()))
    saved = save(client, writing_case, content)
    result = review(client, writing_case, saved)
    assert result.status_code == 409, result.text
    with client.app.state.session_factory() as db:
        assert db.get(Script, UUID(saved["id"])).status == "READY_FOR_REVIEW"
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 0


def test_v3_reader_preserves_legacy_schema_and_explicit_upgrade(client, writing_case):
    # Frozen from the verified task base 1ec47b01, before adding the v3 contract.
    expected = {
        ScriptContent: "b446a3d99d02bc3ae1dc79c2d138af1a6d576b86763be1c52723db7c55a17052",
        Scene: "8bd6c03693e9acbde8ba86ce994f95e9ffa09b97dcd55549b3f9fc7e74b75ea1",
    }
    for model, checksum in expected.items():
        assert (
            hashlib.sha256(
                json.dumps(model.model_json_schema(), sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            == checksum
        )
    content = screenplay(writing_case, capture(client, writing_case))
    parsed = read_script_content(content)
    assert isinstance(parsed, ScriptContentV3)
    assert (
        parsed.model_dump(mode="json")["scenes"][0]["dramatic_beat"]
        == content["scenes"][0]["dramatic_beat"]
    )
    with pytest.raises(ValueError):
        ScriptContent.model_validate(content)
    with pytest.raises(ValueError):
        StoryLedgerContent.model_validate({"schema_version": "2.0"})


def test_v3_director_seed_uses_a_read_only_legacy_text_view(client, writing_case):
    parsed = read_script_content(screenplay(writing_case, capture(client, writing_case)))
    legacy = legacy_script_view(parsed)
    # The old director seed can be serialized and restored with its unchanged schema.
    restored = ScriptContent.model_validate(legacy.model_dump(mode="json"))
    assert restored.scenes[0].dialogue == parsed.scenes[0].dialogue
    assert restored.scenes[0].action == parsed.scenes[0].action
    assert restored.character_assets == parsed.character_assets
    assert restored.schema_version == "2.0"
    assert isinstance(parsed, ScriptContentV3)
    assert parsed.narrative_context.episode_id == UUID(writing_case["episode"]["id"])


def test_waiting_workflow_review_commits_state_before_resume_outbox(client, settings, writing_case):
    from arclight.api.schemas import ReviewCreate
    from arclight.application.commands import resume_run
    from arclight.application.content_dependencies import register_content
    from arclight.application.text_reviews import pause_for_content

    case = writing_case
    content = ScriptContentV3.model_validate(screenplay(case, capture(client, case)))
    factory = client.app.state.session_factory
    with factory.begin() as db:
        actor = db.get(Project, UUID(case["episode"]["project_id"])).owner_id
        run = WorkflowRun(
            id=uuid4(),
            episode_id=UUID(case["episode"]["id"]),
            workflow_version="screenwriter-series/1.0",
            status="WAITING_REVIEW",
            trace_id="state-review",
            idempotency_key="state-review",
            request_fingerprint="0" * 64,
            input_json={
                "target_duration_ms": 30000,
                "narrative_context": content.narrative_context.model_dump(mode="json"),
            },
        )
        db.add(run)
        db.flush()
        script = Script(
            episode_id=run.episode_id,
            run_id=run.id,
            version_no=1,
            content_json=content.model_dump(mode="json"),
            status="READY_FOR_REVIEW",
        )
        db.add(script)
        db.flush()
        run.pending_review = {"resource_type": "script", "version_id": str(script.id)}
        register_content(db, script, actor)
        pause_for_content(db, script, actor)
        run_id, script_id = run.id, script.id
    public_write = client.post(
        f"/api/v1/projects/{case['episode']['project_id']}/story-ledger/versions",
        json={
            "expected_revision": 0,
            "content": {"schema_version": "1.0", "scene_times": [], "entries": []},
        },
        headers={"Idempotency-Key": "public-waiting-write"},
    )
    assert public_write.status_code == 409, public_write.text
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        resume_run(
            db,
            run,
            actor,
            ReviewCreate(
                resource_type="script",
                version_id=script_id,
                decision="APPROVE",
                expected_revision=0,
            ),
            settings,
        )
        assert run.status == "QUEUED" and run.revision == 1
        assert db.scalar(select(StoryLedgerHead)).revision == 1
        assert db.get(Script, script_id).status == "APPROVED"


def test_postgres_competing_review_transactions_have_one_fact_commit(
    client, settings, writing_case
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from arclight.application.text_reviews import complete_manual_review
    from arclight.core.errors import DomainError

    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locking")
    case = writing_case
    context = capture(client, case)
    first = save(client, case, screenplay(case, context))
    second = save(client, case, screenplay(case, context), expected=1)
    factory = client.app.state.session_factory
    with factory() as db:
        actor = db.get(Project, UUID(case["episode"]["project_id"])).owner_id
    ready = Barrier(2)

    def approve(identity):
        ready.wait(timeout=5)
        try:
            with factory.begin() as db:
                result = complete_manual_review(
                    db,
                    db.get(Script, UUID(identity)),
                    actor,
                    settings,
                    decision="APPROVE",
                    comment="",
                    impact_digest=None,
                )
                return result["status"]
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, [first["id"], second["id"]]))
    assert sorted(results) == ["APPROVED", "VERSION_CONFLICT"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 1
        assert db.scalar(select(func.count()).select_from(StoryLedgerApproval)) == 1
        statuses = [db.get(Script, UUID(row["id"])).status for row in [first, second]]
        assert sorted(statuses) == ["APPROVED", "READY_FOR_REVIEW"]


def test_copied_v3_draft_remaps_interactions_and_requires_new_episode_context(client, writing_case):
    case = writing_case
    first = save(client, case, screenplay(case, capture(client, case)))
    assert review(client, case, first).status_code == 200
    result = client.post(
        f"/api/v1/episodes/{case['episode']['id']}/copy",
        json={"episode_no": 2, "title": "重写第二集", "reuse_assets": True},
    )
    assert result.status_code == 201, result.text
    target = result.json()
    response = client.get(f"/api/v1/episodes/{target['id']}/scripts")
    assert response.status_code == 200, response.text
    copied = response.json()[0]
    parsed = read_script_content(copied["content_json"])
    assert parsed.scenes[0].scene_id != first["content_json"]["scenes"][0]["scene_id"]
    copied_case = dict(case, episode=target)
    denied = review(client, copied_case, copied)
    assert denied.status_code == 409, denied.text
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 1


@pytest.mark.parametrize("schema_version", ["2.0", "3.0"])
def test_exact_script_read_keeps_historical_content_and_allows_archived_reads(
    client, writing_case, schema_version
):
    case = writing_case
    content = screenplay(case, capture(client, case))
    if schema_version == "2.0":
        content = ScriptContent(
            title="旧版渡口送信",
            summary="阿青在渡口等待失主。",
            estimated_duration_ms=30000,
            characters=[item["name"] for item in case["characters"]],
            scenes=[Scene(scene_no=1, title="雨夜渡口", action="阿青站在渡口护住信封。")],
        ).model_dump(mode="json")
    first = save(client, case, content)
    newer = copy.deepcopy(content)
    newer["title"] = "下一版渡口送信"
    newer["summary"] = "新稿中的行动尚未批准。"
    second = save(client, case, newer, expected=first["version_no"], base=first["id"])
    prefix = f"/api/v1/episodes/{case['episode']['id']}"
    response = client.get(f"{prefix}/scripts/{first['id']}")
    assert response.status_code == 200, response.text
    assert response.json() == first
    assert response.json()["id"] != second["id"]
    assert response.json()["content_json"]["schema_version"] == schema_version

    archived = client.patch(prefix, json={"archived": True})
    assert archived.status_code == 200, archived.text
    archived_episode = client.get(f"{prefix}/scripts/{first['id']}")
    assert archived_episode.status_code == 200, archived_episode.text
    assert archived_episode.json() == first
    archived = client.patch(
        f"/api/v1/projects/{case['episode']['project_id']}", json={"archived": True}
    )
    assert archived.status_code == 200, archived.text
    archived_project = client.get(f"{prefix}/scripts/{first['id']}")
    assert archived_project.status_code == 200, archived_project.text
    assert archived_project.json() == first
    with client.app.state.session_factory() as db:
        assert db.get(Script, UUID(first["id"])).content_json == first["content_json"]
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 0


def test_exact_script_read_rejects_missing_versions_and_cross_episode_ids(client, writing_case):
    case = writing_case
    saved = save(client, case, screenplay(case, capture(client, case)))
    response = client.post(
        f"/api/v1/projects/{case['episode']['project_id']}/episodes",
        json={"episode_no": 2, "title": "另一集"},
    )
    assert response.status_code == 201, response.text
    other = response.json()
    paths = [
        f"/api/v1/episodes/{case['episode']['id']}/scripts/{uuid4()}",
        f"/api/v1/episodes/{uuid4()}/scripts/{saved['id']}",
        f"/api/v1/episodes/{other['id']}/scripts/{saved['id']}",
    ]
    for path in paths:
        denied = client.get(path)
        assert denied.status_code == 404, denied.text
        assert denied.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "role,expected",
    [("viewer", 200), ("editor", 200), ("outsider", 404), ("unauthenticated", 401)],
)
def test_exact_script_read_obeys_project_membership(client, writing_case, role, expected):
    case = writing_case
    saved = save(client, case, screenplay(case, capture(client, case)))
    owner_header = client.headers["Authorization"]
    if role == "unauthenticated":
        client.headers.pop("Authorization")
    else:
        member = register(client, f"read-{role}@series03.example.com")
        if role != "outsider":
            response = client.post(
                f"/api/v1/projects/{case['episode']['project_id']}/members",
                headers={"Authorization": owner_header},
                json={"email": member["user"]["email"], "role": role},
            )
            assert response.status_code == 201, response.text
    response = client.get(f"/api/v1/episodes/{case['episode']['id']}/scripts/{saved['id']}")
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json() == saved
    else:
        assert response.json()["error"]["code"] == (
            "UNAUTHORIZED" if role == "unauthenticated" else "NOT_FOUND"
        )
