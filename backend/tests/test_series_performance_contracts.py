import copy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from arclight.agents.contracts import (
    ScriptContent,
    ShotContent,
    ShotContentV3,
    StoryboardContent,
    StoryboardContentV3,
    read_content,
    read_shot_content,
    read_storyboard_content,
)


def performance_scene() -> tuple[ScriptContent, dict]:
    prop = {"asset_id": str(uuid4()), "asset_version_id": str(uuid4())}
    script = ScriptContent.model_validate(
        {
            "title": "门口的信",
            "summary": "争信后两人僵持，门外的警告打断了争执。",
            "characters": ["林", "周"],
            "scenes": [
                {
                    "scene_no": 1,
                    "title": "门口",
                    "action": "林夺过信，周震惊后出声阻止。",
                    "props": [prop],
                    "dialogue": [
                        {"speaker": "林", "text": "把信给我。"},
                        {"speaker": "周", "text": "你知道后果吗？"},
                        {"speaker": "周", "text": "别走。"},
                        {"speaker": "旁白", "text": "门外传来脚步声。"},
                    ],
                }
            ],
        }
    )
    lines = script.scenes[0].dialogue

    def person(name: str) -> dict:
        return {
            "character": name,
            "objective": "保住信中的秘密" if name == "周" else "确认真相",
            "emotion_start": "克制",
            "emotion_end": "警觉",
            "action_start": "站立",
            "action_end": "站立",
            "position_start": "门左" if name == "林" else "门右",
            "position_end": "门左" if name == "林" else "门右",
            "eyeline_start": "对方",
            "eyeline_end": "对方",
        }

    def delivery(index: int, presentation: str) -> dict:
        line = lines[index]
        return {
            "line_id": line.line_id,
            "speaker": line.speaker,
            "text": line.text,
            "presentation": presentation,
            "emotion": "压抑",
            "pacing": "停顿后短句",
            "lipsync": "required" if presentation == "visible" else "not_applicable",
            "exemption_reason": "" if presentation == "visible" else "说话者不在画面中",
        }

    shots = []
    for number, line_indexes in ((1, [0]), (2, [1]), (3, [2, 3])):
        props = [
            {
                "asset": prop,
                "holder_start": "周" if number == 1 else "林",
                "holder_end": "林",
                "position_start": "手中",
                "position_end": "手中",
            }
        ]
        people = [person("林"), person("周")] if number < 3 else [person("林")]
        if number == 1:
            people[0]["action_end"] = "握住信"
        if number > 1:
            people[0]["action_start"] = people[0]["action_end"] = "握住信"
        shots.append(
            {
                "schema_version": "3.0",
                "shot_no": number,
                "scene_no": 1,
                "scene_id": script.scenes[0].scene_id,
                "duration_ms": 3000,
                "visual_description": ["林夺信，周后缩", "周的反应", "林在门边聆听"][number - 1],
                "characters": [item["character"] for item in people],
                "props": [prop],
                "dialogue_line_ids": [lines[index].line_id for index in line_indexes],
                "performance": {
                    "scene_purpose": "争信暴露双方目标的冲突",
                    "narrative_purpose": ["争夺", "反应", "悬念"][number - 1],
                    "emotion_rhythm": "克制、爆发、停顿",
                    "characters": people,
                    "interactions": (
                        [{"actor": "林", "target": "周", "action": "夺信", "reaction": "后缩"}]
                        if number == 1
                        else []
                    ),
                    "space_start": "门内",
                    "space_end": "门内",
                    "props": props,
                    "continuity_from_shot": number - 1 if number > 1 else None,
                    "motion_requirement": "dynamic_required" if number < 3 else "static_allowed",
                    "dialogue": [
                        delivery(
                            index,
                            "visible"
                            if number < 3
                            else "off_screen"
                            if index == 2
                            else "voice_over",
                        )
                        for index in line_indexes
                    ],
                },
            }
        )
    return script, {"schema_version": "3.0", "shots": shots, "generation_routes": []}


def test_conflict_reaction_and_three_delivery_types_round_trip():
    script, value = performance_scene()
    board = read_storyboard_content(value).validate_against(script)
    assert isinstance(board, StoryboardContentV3)
    assert board.shots[0].performance.interactions[0].reaction == "后缩"
    assert board.shots[1].performance.narrative_purpose == "反应"
    assert [item.presentation for shot in board.shots for item in shot.performance.dialogue] == [
        "visible",
        "visible",
        "off_screen",
        "voice_over",
    ]
    assert read_content(value) == board.model_dump(mode="json")
    assert all(isinstance(read_shot_content(shot), ShotContentV3) for shot in value["shots"])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("space_start", "屋外", "空间接续"),
        ("continuity_from_shot", None, "明确接续"),
        ("continuity_from_shot", 2, "前序镜头"),
        ("continuity_mode", "ellipsis", "接续原因"),
    ],
)
def test_cross_shot_continuity_is_explicit(field, value, message):
    _, board = performance_scene()
    board["shots"][1]["performance"][field] = value
    with pytest.raises(ValidationError, match=message):
        read_storyboard_content(board)


@pytest.mark.parametrize("subject", ["position", "eyeline", "holder", "prop_version"])
def test_unexplained_character_or_prop_drift_is_rejected(subject):
    _, board = performance_scene()
    shot = board["shots"][1]
    if subject in {"position", "eyeline"}:
        shot["performance"]["characters"][0][f"{subject}_start"] = "窗边"
        shot["performance"]["characters"][0][f"{subject}_end"] = "窗边"
    elif subject == "holder":
        shot["performance"]["props"][0]["holder_start"] = "周"
    else:
        shot["props"][0] = {**shot["props"][0], "asset_version_id": str(uuid4())}
        shot["performance"]["props"][0]["asset"] = shot["props"][0]
    with pytest.raises(ValidationError, match="接续不一致"):
        read_storyboard_content(board)


def test_reasoned_ellipsis_preserves_the_recorded_change():
    _, value = performance_scene()
    current = value["shots"][1]["performance"]
    current["space_start"] = "数秒后门内"
    current["continuity_mode"] = "ellipsis"
    current["continuity_notes"] = "省略双方沉默的三秒，以反应镜头承接"
    board = read_storyboard_content(value)
    assert board.shots[1].performance.continuity_notes == current["continuity_notes"]


@pytest.mark.parametrize("subject", ["position", "eyeline", "holder", "prop_version"])
def test_reaction_cutaway_does_not_erase_last_known_continuity(subject):
    _, value = performance_scene()
    reaction = value["shots"][1]
    reaction["characters"] = ["周"]
    reaction["performance"]["characters"] = [reaction["performance"]["characters"][1]]
    reaction["props"] = []
    reaction["performance"]["props"] = []
    # The actor and letter disappear for a reaction cut, then reappear continuously.
    read_storyboard_content(value)
    returning = value["shots"][2]
    if subject in {"position", "eyeline"}:
        returning["performance"]["characters"][0][f"{subject}_start"] = "窗边"
        returning["performance"]["characters"][0][f"{subject}_end"] = "窗边"
    elif subject == "holder":
        returning["performance"]["props"][0]["holder_start"] = "周"
        returning["performance"]["props"][0]["holder_end"] = "周"
    else:
        returning["props"][0] = {**returning["props"][0], "asset_version_id": str(uuid4())}
        returning["performance"]["props"][0]["asset"] = returning["props"][0]
    with pytest.raises(ValidationError, match="接续不一致"):
        read_storyboard_content(value)
    returning["performance"]["continuity_mode"] = "ellipsis"
    returning["performance"]["continuity_notes"] = "画外转身并交接信件后重新入镜，明确省略中间动作"
    read_storyboard_content(value)


@pytest.mark.parametrize("shot_number", [0, 1])
def test_dynamic_action_or_visible_speech_cannot_be_downgraded(shot_number):
    _, board = performance_scene()
    board["shots"][shot_number]["performance"]["motion_requirement"] = "static_allowed"
    with pytest.raises(ValidationError, match="动态素材"):
        read_storyboard_content(board)


@pytest.mark.parametrize("field", ["text", "speaker", "line_id"])
def test_dialogue_cannot_change_exact_source(field):
    script, board = performance_scene()
    delivery = board["shots"][0]["performance"]["dialogue"][0]
    delivery[field] = str(uuid4()) if field == "line_id" else "替换后的内容"
    if field == "line_id":
        board["shots"][0]["dialogue_line_ids"] = [delivery["line_id"]]
    with pytest.raises(ValueError, match="精确匹配"):
        read_storyboard_content(board).validate_against(script)


@pytest.mark.parametrize("presentation", ["visible", "voice_over", "off_screen"])
def test_lipsync_requires_appropriate_explicit_applicability(presentation):
    _, board = performance_scene()
    delivery = board["shots"][0]["performance"]["dialogue"][0]
    delivery.update(presentation=presentation, lipsync="not_applicable", exemption_reason="")
    with pytest.raises(ValidationError, match="口型"):
        read_storyboard_content(board)


def test_missing_line_is_not_hidden_by_deleting_its_shot_reference():
    script, board = performance_scene()
    board["shots"][2]["performance"]["dialogue"].pop()
    board["shots"][2]["dialogue_line_ids"].pop()
    with pytest.raises(ValueError, match="全部台词"):
        read_storyboard_content(board).validate_against(script)


def test_duplicate_prop_version_is_not_hidden_by_identity_mapping():
    _, board = performance_scene()
    board["shots"][0]["props"].append(copy.deepcopy(board["shots"][0]["props"][0]))
    with pytest.raises(ValidationError, match="镜头道具不能重复"):
        read_storyboard_content(board)


def test_legacy_schema_and_json_are_unchanged_and_cannot_claim_performance():
    legacy = {
        "shots": [{"shot_no": 1, "scene_no": 1, "duration_ms": 1000, "visual_description": "空镜"}]
    }
    original = copy.deepcopy(legacy)
    parsed = read_storyboard_content(legacy)
    assert type(parsed) is StoryboardContent
    assert type(read_shot_content(legacy["shots"][0])) is ShotContent
    assert legacy == original
    assert "performance" not in ShotContent.model_json_schema()["properties"]
    assert StoryboardContent.model_json_schema()["properties"]["schema_version"]["const"] == "2.0"
    _, current = performance_scene()
    with pytest.raises(ValidationError):
        StoryboardContent.model_validate(current)
    with pytest.raises(ValidationError):
        read_storyboard_content({**current, "schema_version": "4.0"})
