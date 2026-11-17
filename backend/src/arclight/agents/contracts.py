"""共享文本合同；旧稿在读取时补确定性标识，不覆写历史 JSON。"""

import copy
import json
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arclight.domain.script_narrative import DramaticBeat, NarrativeContext, StateChangeProposal

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
    estimated_duration_ms: int | None = Field(default=None, strict=True, gt=0)
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


class SceneV3(Scene):
    dramatic_beat: DramaticBeat
    state_changes: list[StateChangeProposal] = Field(max_length=100)

    @model_validator(mode="after")
    def exact_interactions(self) -> "SceneV3":
        lines = {UUID(line.line_id) for line in self.dialogue}
        cited = {
            identity
            for item in self.dramatic_beat.dialogue_interactions
            for identity in item.line_ids
        }
        if not cited <= lines or (lines and cited != lines):
            raise ValueError("Every dialogue line must have a scene-local interaction")
        if len({item.id for item in self.state_changes}) != len(self.state_changes):
            raise ValueError("State proposal identities must be unique")
        return self


class ScriptContentV3(ScriptContent):
    # Explicit subclasses leave legacy JSON schemas and archived component hashes intact.
    schema_version: Literal["3.0"] = "3.0"  # type: ignore[assignment]
    narrative_context: NarrativeContext
    scenes: list[SceneV3] = Field(min_length=1, max_length=50)  # type: ignore[assignment]

    @model_validator(mode="after")
    def exact_narrative(self) -> "ScriptContentV3":
        identities = [item.id for scene in self.scenes for item in scene.state_changes]
        if len(identities) != len(set(identities)):
            raise ValueError("State proposal identities must be unique across the script")
        allowed = set(self.narrative_context.character_ids)
        target = next(
            (
                scene
                for scene in self.scenes
                if scene.scene_no == self.narrative_context.target_scene_no
            ),
            None,
        )
        if (
            target is None
            or target.dramatic_beat.story_time != self.narrative_context.target_story_time
        ):
            raise ValueError("The writing anchor must match the exact delivered scene story time")
        for scene in self.scenes:
            if any(
                item.character_id not in allowed
                for item in scene.dramatic_beat.character_objectives
            ):
                raise ValueError("Scene characters must occur in the frozen writing perspectives")
        return self


def read_script_content(value: dict | ScriptContent) -> ScriptContent:
    """Dispatch stored text explicitly without changing legacy embedded schemas."""
    if isinstance(value, ScriptContentV3) or (
        isinstance(value, dict) and value.get("schema_version") == "3.0"
    ):
        return ScriptContentV3.model_validate(value)
    return ScriptContent.model_validate(value)


def legacy_script_view(content: ScriptContent) -> ScriptContent:
    """Project text for a frozen v2 consumer; never use this view to save a script."""
    if not isinstance(content, ScriptContentV3):
        return content
    value = content.model_dump(
        mode="json",
        exclude={
            "narrative_context": True,
            "scenes": {"__all__": {"dramatic_beat", "state_changes"}},
        },
    )
    value["schema_version"] = "2.0"
    return ScriptContent.model_validate(value)


class ShotContent(Contract):
    shot_no: int = Field(gt=0)
    scene_no: int = Field(gt=0)
    scene_id: Identifier | None = None
    duration_ms: int = Field(strict=True, ge=500, le=30000)
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


class PerformanceCharacter(Contract):
    character: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=1000)
    emotion_start: str = Field(min_length=1, max_length=300)
    emotion_end: str = Field(min_length=1, max_length=300)
    action_start: str = Field(min_length=1, max_length=1000)
    action_end: str = Field(min_length=1, max_length=1000)
    position_start: str = Field(min_length=1, max_length=500)
    position_end: str = Field(min_length=1, max_length=500)
    eyeline_start: str = Field(min_length=1, max_length=500)
    eyeline_end: str = Field(min_length=1, max_length=500)


class PerformanceInteraction(Contract):
    actor: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=1000)
    reaction: str = Field(min_length=1, max_length=1000)


class PerformanceProp(Contract):
    asset: AssetReference
    holder_start: str = Field(min_length=1, max_length=100)
    holder_end: str = Field(min_length=1, max_length=100)
    position_start: str = Field(min_length=1, max_length=500)
    position_end: str = Field(min_length=1, max_length=500)


class PerformanceDialogue(Contract):
    line_id: Identifier
    speaker: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2000)
    presentation: Literal["visible", "voice_over", "off_screen"]
    emotion: str = Field(min_length=1, max_length=300)
    pacing: str = Field(min_length=1, max_length=500)
    lipsync: Literal["required", "not_applicable"]
    exemption_reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def check_lipsync(self) -> "PerformanceDialogue":
        if self.presentation == "visible":
            if self.lipsync != "required" or self.exemption_reason:
                raise ValueError("可见对白必须标记口型适用，不可用豁免代替")
        elif self.lipsync != "not_applicable" or not self.exemption_reason.strip():
            raise ValueError("旁白或画外音必须记录口型不适用的具体理由")
        return self


class PerformanceRoute(Contract):
    # A proposed route is never a capability declaration or execution authorization.
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)


class ShotPerformance(Contract):
    scene_purpose: str = Field(min_length=1, max_length=2000)
    narrative_purpose: str = Field(min_length=1, max_length=2000)
    emotion_rhythm: str = Field(min_length=1, max_length=1000)
    characters: list[PerformanceCharacter] = Field(max_length=50)
    interactions: list[PerformanceInteraction] = Field(default_factory=list, max_length=100)
    space_start: str = Field(min_length=1, max_length=1000)
    space_end: str = Field(min_length=1, max_length=1000)
    props: list[PerformanceProp] = Field(default_factory=list, max_length=50)
    continuity_from_shot: int | None = Field(default=None, gt=0)
    continuity_mode: Literal["continuous", "ellipsis"] = "continuous"
    continuity_notes: str = Field(default="", max_length=2000)
    motion_requirement: Literal["dynamic_required", "static_allowed"]
    dialogue: list[PerformanceDialogue] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def check_motion(self) -> "ShotPerformance":
        names = [item.character for item in self.characters]
        prop_ids = [item.asset.asset_id for item in self.props]
        line_ids = [item.line_id for item in self.dialogue]
        if any(len(values) != len(set(values)) for values in (names, prop_ids, line_ids)):
            raise ValueError("表演角色、道具或台词不能重复")
        for item in self.interactions:
            if item.actor == item.target or not {item.actor, item.target} <= set(names):
                raise ValueError("人物互动必须关联本镜头的两名不同角色")
        changes = (
            self.space_start != self.space_end
            or bool(self.interactions)
            or any(item.presentation == "visible" for item in self.dialogue)
        )
        changes = changes or any(
            item.action_start != item.action_end
            or item.position_start != item.position_end
            or item.eyeline_start != item.eyeline_end
            for item in self.characters
        )
        changes = changes or any(
            item.holder_start != item.holder_end or item.position_start != item.position_end
            for item in self.props
        )
        if changes and self.motion_requirement != "dynamic_required":
            raise ValueError("动作、互动或空间道具变化必须使用动态素材")
        if self.continuity_mode == "ellipsis" and not self.continuity_notes.strip():
            raise ValueError("省略或跳变必须说明接续原因")
        return self


class ShotContentV3(ShotContent):
    schema_version: Literal["3.0"] = "3.0"
    performance: ShotPerformance

    @model_validator(mode="after")
    def check_performance(self) -> "ShotContentV3":
        if {item.character for item in self.performance.characters} != set(self.characters):
            raise ValueError("表演记录必须覆盖全部出镜角色")
        if {item.line_id for item in self.performance.dialogue} != set(self.dialogue_line_ids):
            raise ValueError("表演台词必须精确覆盖镜头台词引用")
        props = {item.asset_id: item for item in self.props}
        if len(props) != len(self.props):
            raise ValueError("镜头道具不能重复或同时引用同一道具的不同版本")
        if {item.asset.asset_id: item.asset for item in self.performance.props} != props:
            raise ValueError("表演道具必须与镜头道具精确版本一致")
        if (
            self.performance.continuity_from_shot is not None
            and self.performance.continuity_from_shot >= self.shot_no
        ):
            raise ValueError("镜头接续必须引用前序镜头")
        return self


class StoryboardContentV3(StoryboardContent):
    # Separate subclasses preserve every archived 2.0 schema and workflow fingerprint.
    schema_version: Literal["3.0"] = "3.0"  # type: ignore[assignment]
    shots: list[ShotContentV3] = Field(min_length=1, max_length=100)  # type: ignore[assignment]
    generation_routes: list[PerformanceRoute] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def check_continuity(self) -> "StoryboardContentV3":
        routes = [(item.provider, item.model) for item in self.generation_routes]
        if len(routes) != len(set(routes)):
            raise ValueError("生成路线不能重复")
        previous: dict[int, ShotContentV3] = {}
        people: dict[int, dict[str, PerformanceCharacter]] = {}
        props: dict[int, dict[str, PerformanceProp]] = {}
        for shot in self.shots:
            prior = previous.get(shot.scene_no)
            performance = shot.performance
            known_people = people.setdefault(shot.scene_no, {})
            known_props = props.setdefault(shot.scene_no, {})
            if prior is None:
                if performance.continuity_from_shot is not None:
                    raise ValueError("场景首镜不能引用其他场景作为连续接续")
            else:
                if performance.continuity_from_shot != prior.shot_no:
                    raise ValueError("同场景镜头必须明确接续前一镜头")
                if performance.continuity_mode == "continuous":
                    self._continuous(prior.performance, performance, known_people, known_props)
            if performance.continuity_mode == "ellipsis":
                known_people.clear()
                known_props.clear()
            known_people.update((item.character, item) for item in performance.characters)
            known_props.update((item.asset.asset_id, item) for item in performance.props)
            previous[shot.scene_no] = shot
        return self

    @staticmethod
    def _continuous(
        prior: ShotPerformance,
        current: ShotPerformance,
        people: dict[str, PerformanceCharacter],
        props: dict[str, PerformanceProp],
    ) -> None:
        if prior.space_end != current.space_start:
            raise ValueError("空间接续不一致；如为合理跳变须显式说明")
        for item in current.characters:
            before = people.get(item.character)
            if before is not None and (
                before.position_end != item.position_start
                or before.eyeline_end != item.eyeline_start
            ):
                raise ValueError("人物走位或视线接续不一致")
        for prop in current.props:
            before_prop = props.get(prop.asset.asset_id)
            if before_prop is not None and (
                before_prop.asset != prop.asset
                or before_prop.holder_end != prop.holder_start
                or before_prop.position_end != prop.position_start
            ):
                raise ValueError("道具版本、归属或位置接续不一致")

    def validate_against(self, script: ScriptContent) -> "StoryboardContentV3":
        scenes = {scene.scene_no: scene for scene in script.scenes}
        bindings = {item.character: item.asset for item in script.character_assets}
        covered: set[str] = set()
        for shot in self.shots:
            scene = scenes.get(shot.scene_no)
            if scene is None or not set(shot.characters) <= set(script.characters):
                raise ValueError("分镜引用了不存在的场景或角色")
            if shot.scene_id is None:
                shot.scene_id = scene.scene_id
            if shot.scene_id != scene.scene_id:
                raise ValueError("场景 ID 与所属场景编号不一致")
            lines = {line.line_id: line for line in scene.dialogue}
            for delivery in shot.performance.dialogue:
                line = lines.get(delivery.line_id)
                if line is None or (delivery.speaker, delivery.text) != (line.speaker, line.text):
                    raise ValueError("表演台词必须精确匹配所选剧本的当前场景及原文")
                if line.line_type == "narration":
                    if delivery.presentation != "voice_over":
                        raise ValueError("旁白必须明确使用 voice_over")
                elif delivery.presentation == "voice_over":
                    raise ValueError("角色对白不得静默改为旁白")
                elif delivery.presentation == "visible" and line.speaker not in shot.characters:
                    raise ValueError("可见对白必须关联出镜说话角色")
                covered.add(line.line_id)
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
            if not set(item.model_dump_json() for item in shot.props) <= set(
                item.model_dump_json() for item in scene.props
            ):
                raise ValueError("镜头道具必须来自剧本场景的精确版本")
        expected_lines = {line.line_id for scene in script.scenes for line in scene.dialogue}
        if covered != expected_lines:
            raise ValueError("表演分镜必须覆盖批准剧本的全部台词")
        if set(scenes) != {shot.scene_no for shot in self.shots}:
            raise ValueError("表演分镜必须覆盖批准剧本的全部场景")
        return self


def read_storyboard_content(value: dict) -> StoryboardContent | StoryboardContentV3:
    if value.get("schema_version") == "3.0":
        return StoryboardContentV3.model_validate(value)
    return StoryboardContent.model_validate(value)


def read_shot_content(value: dict) -> ShotContent | ShotContentV3:
    if value.get("schema_version") == "3.0":
        return ShotContentV3.model_validate(value)
    return ShotContent.model_validate(value)


def read_content(value: dict) -> dict:
    parsed = read_script_content(value) if "scenes" in value else read_storyboard_content(value)
    return parsed.model_dump(mode="json")
