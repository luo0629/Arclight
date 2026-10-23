"""共享文本合同；旧稿在读取时补确定性标识，不覆写历史 JSON。"""

import copy
import json
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[
    str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]


def new_id() -> str:
    return str(uuid4())


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetReference(Contract):
    asset_id: Identifier
    asset_version_id: Identifier


class CharacterAsset(Contract):
    character: str = Field(min_length=1, max_length=100)
    asset: AssetReference


class CharacterPerformance(Contract):
    character: str = Field(min_length=1, max_length=100)
    asset: AssetReference | None = None
    costume: AssetReference | None = None
    action: str = Field(default="", max_length=2000)
    expression: str = Field(default="", max_length=300)
    gaze: str = Field(default="", max_length=300)


class QualityReference(Contract):
    review_id: Identifier
    asset_version_id: Identifier


class Dialogue(Contract):
    line_id: Identifier = Field(default_factory=new_id)
    speaker: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2000)
    line_type: Literal["dialogue", "narration"] = "dialogue"
    emotion: str = Field(default="", max_length=300)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_type(cls, value):
        if isinstance(value, dict) and "line_type" not in value:
            return {
                **value,
                "line_type": "narration" if value.get("speaker") == "旁白" else "dialogue",
            }
        return value

    @model_validator(mode="after")
    def check_type(self) -> "Dialogue":
        if (self.speaker == "旁白") != (self.line_type == "narration"):
            raise ValueError("旁白必须使用 narration，角色对白必须使用 dialogue")
        return self


class Scene(Contract):
    scene_id: Identifier = Field(default_factory=new_id)
    scene_no: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=5000)
    dialogue: list[Dialogue] = Field(default_factory=list, max_length=100)
    scene_asset: AssetReference | None = None
    location_asset: AssetReference | None = None
    props: list[AssetReference] = Field(default_factory=list, max_length=50)
    notes: str = Field(default="", max_length=2000)


class ScriptContent(Contract):
    schema_version: Literal["2.0"] = "2.0"
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=3000)
    characters: list[str] = Field(min_length=1, max_length=50)
    character_assets: list[CharacterAsset] = Field(default_factory=list, max_length=50)
    scenes: list[Scene] = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def read_legacy(cls, value):
        if not isinstance(value, dict) or "schema_version" in value:
            return value
        # 同一旧稿反复读取得到相同 ID；另存之后以显式 ID 延续身份。
        result = copy.deepcopy(value)
        scope = uuid5(
            NAMESPACE_URL, json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        )
        scenes = result.get("scenes", [])
        if not isinstance(scenes, list):
            return result
        for index, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                continue
            scene.setdefault("scene_id", str(uuid5(scope, f"scene:{index}")))
            dialogue = scene.get("dialogue", [])
            if not isinstance(dialogue, list):
                continue
            for position, line in enumerate(dialogue):
                if isinstance(line, dict):
                    line.setdefault("line_id", str(uuid5(scope, f"line:{index}:{position}")))
        return result

    @model_validator(mode="after")
    def check_references(self) -> "ScriptContent":
        if len(set(self.characters)) != len(self.characters) or any(
            not name.strip() or len(name) > 100 for name in self.characters
        ):
            raise ValueError("角色名称不能为空、过长或重复")
        if [s.scene_no for s in self.scenes] != list(range(1, len(self.scenes) + 1)):
            raise ValueError("场景编号必须从 1 连续递增")
        scene_ids = [scene.scene_id for scene in self.scenes]
        line_ids = [line.line_id for scene in self.scenes for line in scene.dialogue]
        bindings = [item.character for item in self.character_assets]
        if len(set(scene_ids)) != len(scene_ids) or len(set(line_ids)) != len(line_ids):
            raise ValueError("场景 ID 和台词 ID 必须唯一")
        if len(set(bindings)) != len(bindings) or not set(bindings) <= set(self.characters):
            raise ValueError("角色资产必须唯一绑定已定义角色")
        for scene in self.scenes:
            for line in scene.dialogue:
                if line.speaker not in [*self.characters, "旁白"]:
                    raise ValueError("对白必须引用已定义角色或旁白")
        return self


class ShotContent(Contract):
    shot_no: int = Field(gt=0)
    scene_no: int = Field(gt=0)
    scene_id: Identifier | None = None
    duration_ms: int = Field(ge=500, le=30000)
    shot_size: Literal["wide", "full", "medium", "close_up"] = "medium"
    visual_description: str = Field(min_length=1, max_length=3000)
    characters: list[str] = Field(default_factory=list, max_length=50)
    character_details: list[CharacterPerformance] = Field(default_factory=list, max_length=50)
    scene_asset: AssetReference | None = None
    location_asset: AssetReference | None = None
    props: list[AssetReference] = Field(default_factory=list, max_length=50)
    camera_movement: Literal["static", "push_in", "pull_out", "pan"] = "static"
    composition: str = Field(default="", max_length=1000)
    dialogue_line_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    sound_effects: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=30
    )
    transition_out: Literal["cut", "fade", "dissolve", "none"] = "cut"
    subtitle: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=2000)
    selected_visual_asset: AssetReference | None = None
    quality_review: QualityReference | None = None

    @model_validator(mode="after")
    def check_details(self) -> "ShotContent":
        details = [item.character for item in self.character_details]
        if len(set(self.characters)) != len(self.characters) or len(set(details)) != len(details):
            raise ValueError("镜头角色及表演记录不能重复")
        if not set(details) <= set(self.characters):
            raise ValueError("表演记录必须关联镜头角色")
        if len(set(self.dialogue_line_ids)) != len(self.dialogue_line_ids):
            raise ValueError("镜头台词引用不能重复")
        if self.quality_review and (
            not self.selected_visual_asset
            or self.quality_review.asset_version_id != self.selected_visual_asset.asset_version_id
        ):
            raise ValueError("质检引用必须与选用素材的精确版本一致")
        return self


class StoryboardContent(Contract):
    schema_version: Literal["2.0"] = "2.0"
    shots: list[ShotContent] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_sequence(self) -> "StoryboardContent":
        if [s.shot_no for s in self.shots] != list(range(1, len(self.shots) + 1)):
            raise ValueError("镜头编号必须从 1 连续递增")
        return self

    def validate_against(self, script: ScriptContent) -> "StoryboardContent":
        scenes = {scene.scene_no: scene for scene in script.scenes}
        bindings = {item.character: item.asset for item in script.character_assets}
        for shot in self.shots:
            scene = scenes.get(shot.scene_no)
            if scene is None or not set(shot.characters) <= set(script.characters):
                raise ValueError("分镜引用了不存在的场景或角色")
            if shot.scene_id is None:
                shot.scene_id = scene.scene_id
            if shot.scene_id != scene.scene_id:
                raise ValueError("场景 ID 与所属场景编号不一致")
            lines = {line.line_id: line for line in scene.dialogue}
            if not set(shot.dialogue_line_ids) <= lines.keys():
                raise ValueError("台词引用必须属于所选剧本的当前场景")
            for line_id in shot.dialogue_line_ids:
                line = lines[line_id]
                if line.line_type == "dialogue" and line.speaker not in shot.characters:
                    raise ValueError("镜头对白必须关联出镜角色")
            details = {item.character: item for item in shot.character_details}
            for character in shot.characters:
                if character in bindings and (
                    character not in details or details[character].asset != bindings[character]
                ):
                    raise ValueError("镜头角色资产必须与剧本精确绑定一致")
            for field in ("scene_asset", "location_asset"):
                expected = getattr(scene, field)
                if expected is not None and getattr(shot, field) != expected:
                    raise ValueError("镜头场景及地点资产必须与剧本精确绑定一致")
        return self


def read_content(value: dict) -> dict:
    contract = ScriptContent if "scenes" in value else StoryboardContent
    return contract.model_validate(value).model_dump(mode="json")
