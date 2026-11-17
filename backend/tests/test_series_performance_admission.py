"""SERIES-04: stored acting plans remain exact and cannot become static delivery."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_text_contract_v2 import legacy_script, make_asset, save_script

from arclight.agents.contracts import (
    ScriptContent,
    ShotContentV3,
    StoryboardContent,
    StoryboardContentV3,
    read_script_content,
    read_storyboard_content,
)
from arclight.application.content_dependencies import register_content
from arclight.application.continuity_selection import validate_selected_shot
from arclight.application.generation_targets import require_available_shot
from arclight.application.production_state import shot_facts
from arclight.application.shot_production import generation_request
from arclight.application.text_reviews import pause_for_run, performance_intent_evidence
from arclight.application.timelines import selected_clip_facts
from arclight.core.errors import DomainError
from arclight.core.image_service import OPENAI_IMAGE_MODEL
from arclight.domain.production_state import ShotPhase
from arclight.domain.production_status import shot_state
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    GenerationJob,
    OutboxEvent,
    Project,
    Review,
    Script,
    Shot,
    Storyboard,
    WorkflowRun,
)


def performance_case(client, episode, *, dynamic=False, with_prop=False, insufficient=False):
    content = legacy_script()
    prop = None
    if with_prop:
        with client.app.state.session_factory.begin() as db:
            prop = make_asset(db, episode["project_id"], "PROP")
        content["scenes"][0]["props"] = [prop]
    script = save_script(client, episode, content)
    parsed = read_script_content(script["content_json"])
    scene = parsed.scenes[0]
    shot = {
        "schema_version": "3.0",
        "shot_no": 1,
        "scene_no": scene.scene_no,
        "scene_id": scene.scene_id,
        "duration_ms": 30000,
        "visual_description": "主角在门边驻足，听见画外的声音。",
        "characters": ["主角"],
        "dialogue_line_ids": [line.line_id for line in scene.dialogue],
        "props": [prop] if prop else [],
        "performance": {
            "scene_purpose": "旧信使主角决定是否进门。",
            "narrative_purpose": "保留主角听见声音后的反应。",
            "emotion_rhythm": "先犹豫，随后坚定。",
            "characters": [
                {
                    "character": "主角",
                    "objective": "确认屋内是否有人。",
                    "emotion_start": "犹豫",
                    "emotion_end": "坚定",
                    "action_start": "驻足",
                    "action_end": "推门" if dynamic else "驻足",
                    "position_start": "门左侧",
                    "position_end": "门左侧",
                    "eyeline_start": "门缝",
                    "eyeline_end": "门缝",
                }
            ],
            "space_start": "门边",
            "space_end": "门边",
            "props": [
                {
                    "asset": prop,
                    "holder_start": "主角",
                    "holder_end": "主角",
                    "position_start": "右手",
                    "position_end": "右手",
                }
            ]
            if prop
            else [],
            "motion_requirement": "dynamic_required" if dynamic else "static_allowed",
            "dialogue": [
                {
                    "line_id": line.line_id,
                    "speaker": line.speaker,
                    "text": line.text,
                    "presentation": "voice_over" if line.line_type == "narration" else "off_screen",
                    "emotion": "克制",
                    "pacing": "一句后留半拍。",
                    "lipsync": "not_applicable",
                    "exemption_reason": "旁白不出镜。"
                    if line.line_type == "narration"
                    else "声音来自门后，说话者嘴部不在画面内。",
                }
                for line in scene.dialogue
            ],
        },
    }
    board = StoryboardContentV3.model_validate(
        {
            "schema_version": "3.0",
            "shots": [shot],
            "generation_routes": []
            if dynamic or insufficient
            else [{"provider": "openai", "model": OPENAI_IMAGE_MODEL}],
        }
    ).validate_against(parsed)
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards",
        json={
            "expected_version_no": 0,
            "script_version_id": script["id"],
            "content": board.model_dump(mode="json"),
        },
    )
    assert response.status_code == 201, response.text
    return script, response.json(), prop


def stored_shot(db, board):
    return db.scalar(select(Shot).where(Shot.storyboard_id == UUID(board["id"])))


def image_capture_payload(episode, board, shot_id):
    return {
        "episode_id": episode["id"],
        "target_type": "SHOT_IMAGE",
        "task_type": "IMAGE",
        "review_target": {"storyboard_id": board["id"], "shot_id": str(shot_id)},
        "inputs": [
            {
                "source_asset_version_id": board["id"],
                "dependency_type": "CONTENT_DEPENDENCY",
                "selectors": ["shot:" + str(shot_id)],
            }
        ],
        "spec": {
            "provider": "mock",
            "model": "mock-image-v1",
            "model_config_version": "series04-admission/1",
            "prompt": "表演意图不能代替生产证据。",
            "prompt_version": "series04-admission/1",
            "output_spec": {"width": 1024, "height": 1024, "format": "png"},
        },
    }


def exact_review(client, episode, board):
    response = client.get(
        f"/api/v1/projects/{episode['project_id']}/human-reviews",
        params={"version_id": board["id"]},
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1 and rows[0]["snapshot"]["version_id"] == board["id"]
    return rows[0]


def test_performance_run_rejects_legacy_source_before_queuing(client, episode):
    script = save_script(client, episode, legacy_script())
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboard-runs",
        headers={"Idempotency-Key": "performance-requires-v3"},
        json={
            "script_version_id": script["id"],
            "professional": True,
            "director_revision_rounds": 2,
            "performance": True,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PERFORMANCE_SCRIPT_V3_REQUIRED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 0
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_dynamic_draft_keeps_plan_but_cannot_be_approved_or_delivered_as_images(client, episode):
    _, board, _ = performance_case(client, episode, dynamic=True)
    prefix = f"/api/v1/episodes/{episode['id']}"
    shots = client.get(prefix + "/shots", params={"storyboard_id": board["id"]}).json()
    assert (
        shots[0]["content_json"]["performance"] == board["content_json"]["shots"][0]["performance"]
    )
    rejected = client.post(
        prefix + f"/storyboards/{board['id']}/review", json={"decision": "APPROVE"}
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "PERFORMANCE_NOT_READY"
    saved = client.get(prefix + "/storyboards").json()
    assert saved[0]["status"] == "READY_FOR_REVIEW"
    with client.app.state.session_factory() as db:
        source = db.get(Storyboard, UUID(board["id"]))
        shot = stored_shot(db, board)
        current_episode = db.get(Episode, UUID(episode["id"]))
        with pytest.raises(DomainError) as request_error:
            generation_request(db, UUID(episode["project_id"]), source, shot)
        assert request_error.value.code == "DYNAMIC_MEDIA_REQUIRED"
        with pytest.raises(DomainError) as timeline_error:
            selected_clip_facts(db, current_episode, shot, uuid4(), 0, 3000)
        assert timeline_error.value.code == "DYNAMIC_MEDIA_REQUIRED"
        facts = shot_facts(db, current_episode, shot)
        assert facts.issues == ["DYNAMIC_MEDIA_REQUIRED"] and facts.media is None
        state = shot_state(facts, source.id, ready=True)
        assert state.phase != ShotPhase.APPROVED and state.issues == ["DYNAMIC_MEDIA_REQUIRED"]
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0


def test_generic_image_capture_cannot_bypass_the_dynamic_requirement(client, episode):
    _, board, _ = performance_case(client, episode, dynamic=True)
    # Model pre-existing approved records in the isolated test database. The real
    # approval endpoint above must still reject this plan's insufficient capability.
    with client.app.state.session_factory.begin() as db:
        source = db.get(Storyboard, UUID(board["id"]))
        source.status = "APPROVED"
        db.get(AssetVersion, source.id).status = "APPROVED"
        shot = stored_shot(db, board)
        shot.status = "APPROVED"
        shot_id = str(shot.id)
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/generation-jobs",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "episode_id": episode["id"],
            "target_type": "SHOT_IMAGE",
            "task_type": "IMAGE",
            "review_target": {"storyboard_id": board["id"], "shot_id": shot_id},
            "inputs": [
                {
                    "source_asset_version_id": board["id"],
                    "dependency_type": "CONTENT_DEPENDENCY",
                    "selectors": ["shot:" + shot_id],
                }
            ],
            "spec": {
                "provider": "mock",
                "model": "mock-image-v1",
                "model_config_version": "series04-admission/1",
                "prompt": "不能将推门动作静默变为静图。",
                "prompt_version": "series04-admission/1",
                "output_spec": {"width": 1024, "height": 1024, "format": "png"},
            },
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DYNAMIC_MEDIA_REQUIRED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0


def test_static_v3_uses_existing_image_request_without_losing_the_plan(client, episode):
    _, board, _ = performance_case(client, episode)
    prefix = f"/api/v1/episodes/{episode['id']}"
    approved = client.post(
        prefix + f"/storyboards/{board['id']}/review", json={"decision": "APPROVE"}
    )
    assert approved.status_code == 200, approved.text
    production = client.get(prefix + f"/storyboards/{board['id']}/production")
    assert production.status_code == 200, production.text
    row = production.json()["shots"][0]
    assert row["blocker"] is None and row["request"]["task_type"] == "IMAGE"
    with client.app.state.session_factory() as db:
        source = db.get(Storyboard, UUID(board["id"]))
        shot = stored_shot(db, board)
        request = generation_request(db, UUID(episode["project_id"]), source, shot)
        assert request.task_type == "IMAGE" and request.target_type == "SHOT_IMAGE"
        assert request.review_target.shot_id == shot.id
        assert shot.content_json["performance"] == board["content_json"]["shots"][0]["performance"]


def test_legacy_storyboard_keeps_its_original_image_request(client, episode):
    script = save_script(client, episode)
    scene = ScriptContent.model_validate(script["content_json"]).scenes[0]
    content = StoryboardContent.model_validate(
        {
            "shots": [
                {
                    "shot_no": 1,
                    "scene_no": 1,
                    "scene_id": scene.scene_id,
                    "duration_ms": 30000,
                    "visual_description": "主角停在门边。",
                    "characters": ["主角"],
                }
            ]
        }
    )
    saved = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards",
        json={
            "expected_version_no": 0,
            "script_version_id": script["id"],
            "content": content.model_dump(mode="json"),
        },
    )
    assert saved.status_code == 201, saved.text
    with client.app.state.session_factory() as db:
        source = db.get(Storyboard, UUID(saved.json()["id"]))
        request = generation_request(
            db, UUID(episode["project_id"]), source, stored_shot(db, saved.json())
        )
        assert request.task_type == "IMAGE"
        assert "performance" not in source.content_json["shots"][0]


@pytest.mark.parametrize("field", ["narrative_purpose", "emotion_rhythm", "motion_requirement"])
def test_reviewed_shot_intent_includes_the_complete_performance_plan(client, episode, field):
    _, board, _ = performance_case(client, episode)
    with client.app.state.session_factory() as db:
        shot = stored_shot(db, board)
        # This comparator receives an already validated review's exact target identity.
        receipt = SimpleNamespace(
            review=SimpleNamespace(
                episode_id=UUID(episode["id"]), target=SimpleNamespace(shot_id=shot.id)
            )
        )
        content = ShotContentV3.model_validate(shot.content_json)
        validate_selected_shot(db, receipt, content, UUID(episode["id"]))
        changed = content.model_dump(mode="json")
        changed["performance"][field] = (
            "dynamic_required" if field == "motion_requirement" else "改为另一项表演意图。"
        )
        with pytest.raises(DomainError) as error:
            validate_selected_shot(
                db, receipt, ShotContentV3.model_validate(changed), UUID(episode["id"])
            )
        assert error.value.code == "REVIEW_TARGET_INVALID"
        assert shot.content_json == content.model_dump(mode="json")


@pytest.mark.parametrize("reuse_assets", [False, True])
def test_copy_rebinds_nested_dialogue_and_preserves_or_clears_exact_props(
    client, episode, reuse_assets
):
    script, board, prop = performance_case(client, episode, dynamic=True, with_prop=True)
    copied = client.post(
        f"/api/v1/episodes/{episode['id']}/copy",
        json={"episode_no": 2, "title": "新集", "reuse_assets": reuse_assets},
    )
    assert copied.status_code == 201, copied.text
    prefix = f"/api/v1/episodes/{copied.json()['id']}"
    (copied_script,) = client.get(prefix + "/scripts").json()
    (copied_board,) = client.get(prefix + "/storyboards").json()
    parsed_script = read_script_content(copied_script["content_json"])
    parsed_board = read_storyboard_content(copied_board["content_json"]).validate_against(
        parsed_script
    )
    assert isinstance(parsed_board, StoryboardContentV3)
    before = board["content_json"]["shots"][0]
    after = copied_board["content_json"]["shots"][0]
    old_lines = {line["line_id"] for line in script["content_json"]["scenes"][0]["dialogue"]}
    new_lines = {line.line_id for line in parsed_script.scenes[0].dialogue}
    assert not old_lines & new_lines
    assert set(after["dialogue_line_ids"]) == new_lines
    assert {line["line_id"] for line in after["performance"]["dialogue"]} == new_lines
    assert after["performance"]["characters"] == before["performance"]["characters"]
    assert after["props"] == ([prop] if reuse_assets else [])
    assert [item["asset"] for item in after["performance"]["props"]] == after["props"]
    assert copied_board["status"] == "READY_FOR_REVIEW"
    originals = client.get(f"/api/v1/episodes/{episode['id']}/storyboards").json()
    assert originals[0]["content_json"] == board["content_json"]


def test_manual_save_rejects_changed_exact_dialogue_and_lossy_version_downgrade(client, episode):
    script, board, _ = performance_case(client, episode)
    prefix = f"/api/v1/episodes/{episode['id']}"
    body = {
        "expected_version_no": 1,
        "base_version_id": board["id"],
        "script_version_id": script["id"],
        "content": deepcopy(board["content_json"]),
    }
    body["content"]["shots"][0]["performance"]["dialogue"][0]["text"] = "不能伪造的台词。"
    invalid = client.post(prefix + "/storyboards", json=body)
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["error"]["code"] == "INVALID_REFERENCES"
    old = deepcopy(board["content_json"])
    old["schema_version"] = "2.0"
    old.pop("generation_routes")
    for shot in old["shots"]:
        shot.pop("schema_version")
        shot.pop("performance")
    body["content"] = old
    downgraded = client.post(prefix + "/storyboards", json=body)
    assert downgraded.status_code == 422, downgraded.text
    assert downgraded.json()["error"]["code"] == "STORYBOARD_VERSION_DOWNGRADE"
    versions = client.get(prefix + "/storyboards").json()
    assert len(versions) == 1 and versions[0]["content_json"] == board["content_json"]


@pytest.mark.parametrize("dynamic", [False, True])
def test_intent_only_approval_persists_and_never_admits_media(client, episode, dynamic):
    script, board, _ = performance_case(client, episode, dynamic=dynamic, insufficient=True)
    prefix = f"/api/v1/episodes/{episode['id']}"
    reason = "保留表演意图；待受控动作、精确参考和口型能力具备后另存重审。"
    response = client.post(
        prefix + f"/storyboards/{board['id']}/review",
        json={"decision": "APPROVE", "performance_intent_only": True, "comment": reason},
    )
    assert response.status_code == 200, response.text
    intent = response.json()["performance_intent"]
    assert intent["scope"] == "INTENT_ONLY" and intent["production_ready"] is False
    assert intent["source_script_version_id"] == script["id"]
    assert intent["source_script_sha256"] == digest(script["content_json"])
    assert "PERFORMANCE_CAPABILITY_UNSUPPORTED" in {issue["code"] for issue in intent["blockers"]}
    ledger = exact_review(client, episode, board)
    assert ledger["id"] == response.json()["human_review_request_id"]
    assert ledger["status"] == "RESUMED"
    assert ledger["decision"]["decision_json"]["comment"] == reason
    assert ledger["decision"]["decision_json"]["performance_intent"] == intent
    assert exact_review(client, episode, board) == ledger
    old = exact_review(client, episode, script)
    assert "performance_intent" not in old["decision"]["decision_json"]
    saved = client.get(prefix + "/storyboards").json()[0]
    assert saved["status"] == "APPROVED" and saved["content_json"] == board["content_json"]
    code = "DYNAMIC_MEDIA_REQUIRED" if dynamic else "PERFORMANCE_NOT_READY"
    production = client.get(prefix + f"/storyboards/{board['id']}/production")
    assert production.status_code == 200, production.text
    item = production.json()["shots"][0]
    assert item["request"] is None and item["blocker"]["code"] == code
    with client.app.state.session_factory() as db:
        source = db.get(Storyboard, UUID(board["id"]))
        shot = stored_shot(db, board)
        current_episode = db.get(Episode, UUID(episode["id"]))
        binding = {"storyboard_id": board["id"], "shot_id": str(shot.id)}
        # Read context remains available, while every explicit media task stays blocked.
        require_available_shot(db, current_episode.project_id, binding, current_episode.id)
        for task_type in ("IMAGE", "VIDEO"):
            with pytest.raises(DomainError) as media_error:
                require_available_shot(
                    db, current_episode.project_id, binding, current_episode.id, task_type=task_type
                )
            expected = code if task_type == "IMAGE" else "PERFORMANCE_NOT_READY"
            assert media_error.value.code == expected
        with pytest.raises(DomainError) as request_error:
            generation_request(db, current_episode.project_id, source, shot)
        assert request_error.value.code == code
        facts = shot_facts(db, current_episode, shot)
        assert facts.issues == [code] and facts.media is None
        assert shot_state(facts, source.id, ready=True).phase != ShotPhase.APPROVED
        shot_id = shot.id
    captured = client.post(
        f"/api/v1/projects/{episode['project_id']}/generation-jobs",
        headers={"Idempotency-Key": str(uuid4())},
        json=image_capture_payload(episode, board, shot_id),
    )
    assert captured.status_code == 409 and captured.json()["error"]["code"] == code
    timeline = client.post(
        prefix + "/timelines",
        json={
            "creation_key": str(uuid4()),
            "storyboard_id": board["id"],
            "selected_shots": [{"shot_id": str(shot_id), "asset_version_id": str(uuid4())}],
        },
    )
    assert timeline.status_code == 409 and timeline.json()["error"]["code"] == code
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 0
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"decision": "APPROVE", "comment": "普通理由不是明确接受能力缺口"}, 409),
        ({"decision": "APPROVE", "performance_intent_only": True, "comment": "  "}, 422),
        ({"decision": "REJECT", "performance_intent_only": True, "comment": "不能混用"}, 422),
        (
            {"decision": "APPROVE", "performance_intent_only": "true", "comment": "不能隐式转换"},
            422,
        ),
    ],
)
def test_intent_only_requires_explicit_approval_and_reason(client, episode, payload, status):
    _, board, _ = performance_case(client, episode, dynamic=True)
    with client.app.state.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(HumanReviewDecision))
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards/{board['id']}/review", json=payload
    )
    assert response.status_code == status, response.text
    assert exact_review(client, episode, board)["decision"] is None
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(HumanReviewDecision)) == before
        assert db.get(Storyboard, UUID(board["id"])).status == "READY_FOR_REVIEW"


@pytest.mark.parametrize("corruption", ["exact_dialogue", "dialogue_order"])
def test_intent_confirmation_cannot_waive_structural_errors(client, episode, corruption):
    script, board, _ = performance_case(client, episode, dynamic=True)
    content = deepcopy(board["content_json"])
    shot = content["shots"][0]
    if corruption == "exact_dialogue":
        shot["performance"]["dialogue"][0]["text"] = "不能通过人工确认篡改台词。"
    else:
        shot["dialogue_line_ids"].reverse()
        shot["performance"]["dialogue"].reverse()
    with client.app.state.session_factory.begin() as db:
        # Insert a separate pre-existing bad version; the valid source and its
        # immutable history remain intact, including the database triggers.
        invalid = Storyboard(
            episode_id=UUID(episode["id"]),
            run_id=None,
            script_version_id=UUID(script["id"]),
            version_no=2,
            content_json=content,
            status="READY_FOR_REVIEW",
        )
        db.add(invalid)
        db.flush()
        invalid_id = str(invalid.id)
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards/{invalid_id}/review",
        json={
            "decision": "APPROVE",
            "performance_intent_only": True,
            "comment": "人工理由不能免除精确引用及台词顺序",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PERFORMANCE_NOT_READY"
    with client.app.state.session_factory() as db:
        assert db.get(Storyboard, UUID(invalid_id)).status == "READY_FOR_REVIEW"
        assert db.get(Storyboard, UUID(board["id"])).content_json == board["content_json"]
        assert not db.scalar(
            select(HumanReviewDecision)
            .join(HumanReviewRequest, HumanReviewRequest.id == HumanReviewDecision.request_id)
            .where(HumanReviewRequest.creation_key == "text:" + invalid_id)
        )


@pytest.mark.parametrize("forgery", ["blockers", "source", "reason", "production_ready"])
def test_unified_intent_decision_rejects_forged_receipt(client, episode, forgery):
    _, board, _ = performance_case(client, episode, dynamic=True)
    row = exact_review(client, episode, board)
    with client.app.state.session_factory() as db:
        evidence = performance_intent_evidence(
            db, db.get(Storyboard, UUID(board["id"])), "APPROVE", "明确接受意图但保留阻塞"
        ).model_dump(mode="json")
    comment = "明确接受意图但保留阻塞"
    if forgery == "blockers":
        evidence["blockers"] = []
    elif forgery == "source":
        evidence["source_script_sha256"] = "0" * 64
    elif forgery == "reason":
        comment = " "
    else:
        evidence["production_ready"] = True
    response = client.post(
        f"/api/v1/human-reviews/{row['id']}/decisions",
        json={
            "version_id": board["id"],
            "request_sha256": row["request_sha256"],
            "decision": "APPROVE",
            "comment": comment,
            "performance_intent": evidence,
        },
    )
    assert response.status_code == (409 if forgery in {"blockers", "source"} else 422), (
        response.text
    )
    assert exact_review(client, episode, board)["decision"] is None


@pytest.mark.parametrize("change", [None, "source", "blockers"])
def test_unified_intent_decision_replay_rechecks_source_and_blockers(
    client, episode, monkeypatch, change
):
    script, board, _ = performance_case(client, episode, dynamic=True)
    row = exact_review(client, episode, board)
    with client.app.state.session_factory() as db:
        evidence = performance_intent_evidence(
            db, db.get(Storyboard, UUID(board["id"])), "APPROVE", "保留意图并等待另存重审"
        ).model_dump(mode="json")
        before_resumes = db.scalar(select(func.count()).select_from(HumanReviewResume))
    payload = {
        "version_id": board["id"],
        "request_sha256": row["request_sha256"],
        "decision": "APPROVE",
        "comment": "保留意图并等待另存重审",
        "performance_intent": evidence,
    }
    url = f"/api/v1/human-reviews/{row['id']}"
    saved = client.post(url + "/decisions", json=payload)
    assert saved.status_code == 200, saved.text
    assert client.post(url + "/decisions", json=payload).json() == saved.json()
    if change == "source":
        original_get = Session.get

        def changed_source(session, entity, identity, **kwargs):
            current = original_get(session, entity, identity, **kwargs)
            if entity is Script and current is not None and current.id == UUID(script["id"]):
                # Simulate an authoritative read changing across the saved decision,
                # without issuing any UPDATE against the immutable source history.
                return SimpleNamespace(
                    id=current.id,
                    episode_id=current.episode_id,
                    status=current.status,
                    content_json={**current.content_json, "summary": "当前来源正文已变化"},
                )
            return current

        monkeypatch.setattr(Session, "get", changed_source)
    elif change == "blockers":
        from arclight.application import director_performance

        checker = director_performance.check_performance_storyboard

        def changed_blockers(*args, **kwargs):
            issues = checker(*args, **kwargs)
            return [issues[0].model_copy(update={"message": "当前能力事实已变化"}), *issues[1:]]

        monkeypatch.setattr(director_performance, "check_performance_storyboard", changed_blockers)
    replay = client.post(url + "/decisions", json=payload)
    resumed = client.post(
        url + "/resume",
        json={"decision_id": saved.json()["id"], "request_sha256": row["request_sha256"]},
    )
    if change is not None:
        for response in (replay, resumed):
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "PERFORMANCE_INTENT_CHANGED"
        with client.app.state.session_factory() as db:
            assert db.get(Storyboard, UUID(board["id"])).status == "READY_FOR_REVIEW"
            assert db.scalar(select(func.count()).select_from(HumanReviewResume)) == before_resumes
            assert (
                db.scalar(select(Script).where(Script.id == UUID(script["id"]))).content_json
                == script["content_json"]
            )
    else:
        assert replay.json() == saved.json()
        assert resumed.status_code == 200, resumed.text
        again = client.post(
            url + "/resume",
            json={"decision_id": saved.json()["id"], "request_sha256": row["request_sha256"]},
        )
        assert again.json() == resumed.json()
        assert (
            exact_review(client, episode, board)["decision"]["decision_json"]["performance_intent"]
            == evidence
        )
        with client.app.state.session_factory() as db:
            assert (
                db.scalar(select(func.count()).select_from(HumanReviewResume)) == before_resumes + 1
            )
            assert db.get(Storyboard, UUID(board["id"])).status == "APPROVED"


@pytest.mark.parametrize("entry", ["run", "human_review"])
@pytest.mark.parametrize("episode_target", [30000, 60000])
def test_workflow_review_create_persists_intent_and_queues_exact_handoff_once(
    client, episode, entry, episode_target
):
    script, manual, _ = performance_case(client, episode, dynamic=True)
    factory = client.app.state.session_factory
    # Seed an already-produced legacy workflow's pending V3 artifact, then use
    # the real pause service so both entry points consume the same durable review.
    with factory.begin() as db:
        owner = db.get(Project, UUID(episode["project_id"])).owner_id
        run = WorkflowRun(
            episode_id=UUID(episode["id"]),
            workflow_type="storyboard",
            workflow_version="director/1.0",
            status="WAITING_REVIEW",
            trace_id="series04-workflow-intent",
            idempotency_key="series04-workflow-intent",
            request_fingerprint="0" * 64,
            input_json={
                "script_version_id": script["id"],
                "script": script["content_json"],
                "target_duration_ms": 30000,
            },
        )
        db.add(run)
        db.flush()
        board = Storyboard(
            episode_id=UUID(episode["id"]),
            run_id=run.id,
            script_version_id=UUID(script["id"]),
            version_no=2,
            content_json=deepcopy(manual["content_json"]),
        )
        db.add(board)
        db.flush()
        content = board.content_json["shots"][0]
        db.add(
            Shot(
                storyboard_id=board.id,
                shot_no=content["shot_no"],
                duration_ms=content["duration_ms"],
                description=content["visual_description"],
                content_json=deepcopy(content),
            )
        )
        register_content(db, board, owner)
        run.pending_review = {"resource_type": "storyboard", "version_id": str(board.id)}
        pause_for_run(db, run)
        run_id, board_id = str(run.id), str(board.id)
    # Simulate external drift in the isolated fixture after the run and review
    # snapshots were frozen; normal API edits are blocked while the run is active.
    if episode_target != 30000:
        with factory.begin() as db:
            db.get(Episode, UUID(episode["id"])).target_duration_ms = episode_target
    pending = exact_review(client, episode, {"id": board_id})
    assert pending["snapshot"]["run_id"] == run_id
    assert pending["decision"] is None and pending["resume"] is None
    comment = "仅确认表演意图，制作阻断继续保留"
    if entry == "run":
        url = f"/api/v1/runs/{run_id}/resume"
        payload = {
            "resource_type": "storyboard",
            "version_id": board_id,
            "expected_revision": 0,
            "decision": "APPROVE",
            "comment": comment,
        }
        explicit_intent = {"performance_intent_only": True}
    else:
        url = f"/api/v1/human-reviews/{pending['id']}/decisions"
        payload = {
            "version_id": board_id,
            "request_sha256": pending["request_sha256"],
            "decision": "APPROVE",
            "comment": comment,
        }
        with factory() as db:
            evidence = performance_intent_evidence(
                db, db.get(Storyboard, UUID(board_id)), "APPROVE", comment
            ).model_dump(mode="json")
        explicit_intent = {"performance_intent": evidence}
    ordinary = client.post(url, json=payload)
    assert ordinary.status_code == 409, ordinary.text
    assert ordinary.json()["error"]["code"] == "PERFORMANCE_NOT_READY"
    invalid = client.post(url, json={**payload, **explicit_intent, "comment": " "})
    assert invalid.status_code == 422, invalid.text
    assert exact_review(client, episode, {"id": board_id})["decision"] is None
    accepted_payload = {**payload, **explicit_intent}
    accepted = client.post(url, json=accepted_payload)
    assert accepted.status_code == (202 if entry == "run" else 200), accepted.text
    if entry == "run":
        assert accepted.json()["status"] == "QUEUED" and accepted.json()["revision"] == 1
    else:
        repeated = client.post(url, json=accepted_payload)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == accepted.json()
        decided = exact_review(client, episode, {"id": board_id})
        assert decided["decision"]["decision_json"]["performance_intent"] == evidence
        assert decided["resume"] is None
        with factory() as db:
            assert db.get(Storyboard, UUID(board_id)).status == "READY_FOR_REVIEW"
            assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
            assert db.scalar(select(func.count()).select_from(Review)) == 0
        resumed = client.post(
            f"/api/v1/human-reviews/{pending['id']}/resume",
            json={
                "decision_id": accepted.json()["id"],
                "request_sha256": pending["request_sha256"],
            },
        )
        assert resumed.status_code == 200, resumed.text
    ledger = exact_review(client, episode, {"id": board_id})
    receipt = ledger["decision"]["decision_json"]["performance_intent"]
    assert receipt["scope"] == "INTENT_ONLY" and receipt["production_ready"] is False
    assert receipt["source_script_version_id"] == script["id"]
    assert receipt["source_script_sha256"] == digest(script["content_json"])
    assert receipt["blockers"] and ledger["snapshot"]["run_id"] == run_id
    assert ledger["decision"]["decision_json"]["comment"] == comment
    replay = client.post(
        f"/api/v1/human-reviews/{ledger['id']}/resume",
        json={
            "decision_id": ledger["decision"]["id"],
            "request_sha256": ledger["request_sha256"],
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == ledger["resume"]
    assert exact_review(client, episode, {"id": board_id}) == ledger
    with factory() as db:
        stored = db.get(WorkflowRun, UUID(run_id))
        assert stored.resume_json["performance_intent_only"] is True
        assert stored.pending_review is None
        assert stored.status == "QUEUED" and stored.revision == 1
        assert stored.input_json["target_duration_ms"] == 30000
        assert db.get(Episode, UUID(episode["id"])).target_duration_ms == episode_target
        approved = db.get(Storyboard, UUID(board_id))
        assert approved.status == "APPROVED" and approved.content_json == manual["content_json"]
        shot = stored_shot(db, {"id": board_id})
        assert shot.duration_ms == 30000
        with pytest.raises(DomainError) as blocked:
            generation_request(db, UUID(episode["project_id"]), approved, shot)
        assert blocked.value.code == "DYNAMIC_MEDIA_REQUIRED"
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert db.scalar(select(func.count()).select_from(Review)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(HumanReviewDecision)
                .where(HumanReviewDecision.request_id == UUID(ledger["id"]))
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(HumanReviewResume)
                .where(HumanReviewResume.request_id == UUID(ledger["id"]))
            )
            == 1
        )
