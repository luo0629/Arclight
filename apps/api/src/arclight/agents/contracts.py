from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Dialogue(Contract):
    speaker: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2000)


class Scene(Contract):
    scene_no: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=5000)
    dialogue: list[Dialogue] = Field(default_factory=list, max_length=100)


class ScriptContent(Contract):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=3000)
    characters: list[str] = Field(min_length=1, max_length=50)
    scenes: list[Scene] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def check_references(self) -> "ScriptContent":
        if len(set(self.characters)) != len(self.characters):
            raise ValueError("角色名称不能重复")
        if [s.scene_no for s in self.scenes] != list(range(1, len(self.scenes) + 1)):
            raise ValueError("场景编号必须从 1 连续递增")
        for scene in self.scenes:
            for line in scene.dialogue:
                if line.speaker not in [*self.characters, "旁白"]:
                    raise ValueError("对白必须引用已定义角色或旁白")
        return self


class ShotContent(Contract):
    shot_no: int = Field(gt=0)
    scene_no: int = Field(gt=0)
    duration_ms: int = Field(ge=500, le=30000)
    shot_size: Literal["wide", "full", "medium", "close_up"] = "medium"
    visual_description: str = Field(min_length=1, max_length=3000)
    characters: list[str] = Field(default_factory=list, max_length=50)
    camera_movement: Literal["static", "push_in", "pull_out", "pan"] = "static"
    subtitle: str = Field(default="", max_length=2000)


class StoryboardContent(Contract):
    shots: list[ShotContent] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_sequence(self) -> "StoryboardContent":
        if [s.shot_no for s in self.shots] != list(range(1, len(self.shots) + 1)):
            raise ValueError("镜头编号必须从 1 连续递增")
        return self

    def validate_against(self, script: ScriptContent) -> "StoryboardContent":
        scene_nos = {scene.scene_no for scene in script.scenes}
        for shot in self.shots:
            if shot.scene_no not in scene_nos or not set(shot.characters) <= set(script.characters):
                raise ValueError("分镜引用了不存在的场景或角色")
        return self
