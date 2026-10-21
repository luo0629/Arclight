import pytest
from arclight.agents.contracts import Dialogue, Scene, ScriptContent, ShotContent, StoryboardContent
from arclight.providers.text import MockTextProvider
from pydantic import ValidationError


def test_contract_rejects_unknown_character():
    with pytest.raises(ValidationError):
        ScriptContent(
            title="标题",
            summary="摘要",
            characters=["主角"],
            scenes=[
                Scene(
                    scene_no=1,
                    title="场景",
                    action="动作",
                    dialogue=[Dialogue(speaker="不存在的角色", text="对白")],
                )
            ],
        )


def test_storyboard_requires_valid_scene_and_sequence():
    script = MockTextProvider().script("故事", {})
    with pytest.raises(ValidationError):
        StoryboardContent(
            shots=[ShotContent(shot_no=2, scene_no=1, duration_ms=1000, visual_description="动作")]
        )
    with pytest.raises(ValueError):
        StoryboardContent(
            shots=[ShotContent(shot_no=1, scene_no=9, duration_ms=1000, visual_description="动作")]
        ).validate_against(script)
