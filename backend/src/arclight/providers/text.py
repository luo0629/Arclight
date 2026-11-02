import json
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel

from arclight.agents.contracts import (
    CharacterPerformance,
    Scene,
    ScriptContent,
    ShotContent,
    StoryboardContent,
)
from arclight.agents.script_rewrite import RewriteResult
from arclight.agents.story_analysis import KeyEvent, StoryAnalysisContent
from arclight.agents.story_outline import OutlineEpisode, StoryOutlineContent
from arclight.application.content_duration import duration_bounds
from arclight.core.config import Settings
from arclight.domain.context import check_call
from arclight.domain.project_settings import ProjectStorySettings
from arclight.providers.prompts import TEXT_MAX_TOKENS, TEXT_PROMPTS

T = TypeVar("T", bound=BaseModel)


class TextProvider(Protocol):
    def rewrite(self, context: dict) -> RewriteResult: ...

    def outline(self, source: dict) -> StoryOutlineContent: ...

    def analyze(self, story: str) -> StoryAnalysisContent: ...

    def script(
        self,
        story: str,
        style: dict,
        duration_ms: int = 30000,
        *,
        story_settings: dict | None = None,
        story_context: dict | None = None,
        outline_context: dict | None = None,
    ) -> ScriptContent: ...

    def storyboard(self, script: ScriptContent, duration_ms: int) -> StoryboardContent: ...


class MockTextProvider:
    """只用于本地连通性验证，不调用模型，也不声称具有创作能力。"""

    def rewrite(self, context: dict) -> RewriteResult:
        import copy

        scenes = copy.deepcopy(context.get("scenes", []))
        instructions = context["instructions"]
        for scene in scenes:
            scene["action"] = "演示改写（Mock）：" + scene["action"][:4900]
            for line in scene["dialogue"]:
                if line["line_type"] == "dialogue" and instructions["dialogue_max_chars"]:
                    line["text"] = line["text"][: instructions["dialogue_max_chars"]]
            if instructions["narration_max_percent"] is not None:
                scene["dialogue"] = [
                    line for line in scene["dialogue"] if line["line_type"] != "narration"
                ]
        text = None
        if "text" in context:
            text = "演示改写（Mock）：" + context["text"][:1900]
            if context["target"]["field"] == "dialogue" and instructions["dialogue_max_chars"]:
                text = text[: instructions["dialogue_max_chars"]]
        return RewriteResult(
            estimated_duration_ms=context["target_duration_ms"], scenes=scenes, text=text
        )

    def outline(self, source: dict) -> StoryOutlineContent:
        return StoryOutlineContent(
            schema_version="1.0",
            target_episode_count=source["target_episode_count"],
            episode_duration_ms=source["episode_duration_ms"],
            episodes=[
                OutlineEpisode(
                    episode_no=i,
                    title=f"演示第{i}集（Mock）",
                    goal="演示目标：" + source["story"].strip()[:200],
                    conflict="演示冲突（Mock，待人工改写）",
                    turning_point="演示转折（Mock，待人工改写）",
                    ending_hook="演示结尾钩子（Mock，待人工改写）",
                )
                for i in range(1, source["target_episode_count"] + 1)
            ],
        )

    def analyze(self, story: str) -> StoryAnalysisContent:
        return StoryAnalysisContent(
            schema_version="1.0",
            summary="演示提炼（Mock，不代表模型分析）：" + story.strip()[:1000],
            conflicts=[],
            characters=[],
            key_events=[
                KeyEvent(
                    title="原文片段（Mock）", description=story.strip()[:2000], character_names=[]
                )
            ],
        )

    def script(
        self,
        story: str,
        style: dict,
        duration_ms: int = 30000,
        *,
        story_settings: dict | None = None,
        story_context: dict | None = None,
        outline_context: dict | None = None,
    ) -> ScriptContent:
        return ScriptContent(
            title="演示剧本（Mock）",
            estimated_duration_ms=duration_ms,
            summary=(outline_context["episode"]["goal"] if outline_context else story[:500]),
            characters=["主角"],
            scenes=[Scene(scene_no=1, title="故事现场", action=story[:5000])],
        )

    def storyboard(self, script: ScriptContent, duration_ms: int) -> StoryboardContent:
        count = max(5, (duration_ms + 29999) // 30000)
        duration, remainder = divmod(duration_ms, count)
        return StoryboardContent(
            shots=[
                ShotContent(
                    shot_no=i,
                    scene_no=script.scenes[0].scene_no,
                    scene_id=script.scenes[0].scene_id,
                    scene_asset=script.scenes[0].scene_asset,
                    location_asset=script.scenes[0].location_asset,
                    props=script.scenes[0].props,
                    dialogue_line_ids=[line.line_id for line in script.scenes[0].dialogue]
                    if i == 1
                    else [],
                    character_details=[
                        CharacterPerformance(character=item.character, asset=item.asset)
                        for item in script.character_assets
                    ],
                    duration_ms=duration + (1 if i <= remainder else 0),
                    visual_description=f"演示镜头 {i}：{script.scenes[0].action[:200]}",
                    characters=script.characters,
                )
                for i in range(1, count + 1)
            ]
        )


class OpenAICompatibleTextProvider:
    def __init__(self, settings: Settings, contexts: dict[str, dict] | None = None):
        self.settings = settings
        self.contexts = contexts or {}

    def _generate(self, contract: type[T], instruction: str, data: dict) -> T:
        stage = {
            ScriptContent: "script",
            StoryboardContent: "storyboard",
            RewriteResult: "rewrite",
            StoryAnalysisContent: "analyze",
            StoryOutlineContent: "outline",
        }[contract]
        if stage in self.contexts:
            data = check_call(self.contexts[stage], data)
        # 输入作为独立数据发送；不允许模型输出执行命令或写入数据库。
        with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
            response = client.post(
                f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.llm_api_key.get_secret_value()}"},
                json={
                    "model": self.settings.llm_model,
                    "max_tokens": TEXT_MAX_TOKENS,
                    "messages": [
                        {"role": "system", "content": instruction},
                        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": contract.__name__,
                            "schema": contract.model_json_schema(),
                        },
                    },
                },
            )
            response.raise_for_status()
            return contract.model_validate_json(response.json()["choices"][0]["message"]["content"])

    def rewrite(self, context: dict) -> RewriteResult:
        return self._generate(
            RewriteResult,
            TEXT_PROMPTS["rewrite"],
            context,
        )

    def outline(self, source: dict) -> StoryOutlineContent:
        return self._generate(
            StoryOutlineContent,
            TEXT_PROMPTS["outline"],
            source,
        )

    def analyze(self, story: str) -> StoryAnalysisContent:
        return self._generate(
            StoryAnalysisContent,
            TEXT_PROMPTS["analyze"],
            {"story": story},
        )

    def script(
        self,
        story: str,
        style: dict,
        duration_ms: int = 30000,
        *,
        story_settings: dict | None = None,
        story_context: dict | None = None,
        outline_context: dict | None = None,
    ) -> ScriptContent:
        return self._generate(
            ScriptContent,
            TEXT_PROMPTS["script"],
            {
                "story": story,
                "story_settings": ProjectStorySettings.model_validate(
                    story_settings or {}
                ).model_dump(),
                "story_context": story_context or {"schema_version": 1, "entries": []},
                "outline_context": outline_context,
                "style_guide": style,
                "target_duration_ms": duration_ms,
                "duration_range_ms": duration_bounds(
                    duration_ms, self.settings.text_duration_tolerance_percent
                ),
            },
        )

    def storyboard(self, script: ScriptContent, duration_ms: int) -> StoryboardContent:
        return self._generate(
            StoryboardContent,
            TEXT_PROMPTS["storyboard"],
            {
                "script": script.model_dump(),
                "target_duration_ms": duration_ms,
                "duration_range_ms": duration_bounds(
                    duration_ms, self.settings.text_duration_tolerance_percent
                ),
            },
        ).validate_against(script)


def build_provider(settings: Settings) -> TextProvider:
    if settings.provider == "mock":
        return MockTextProvider()
    return OpenAICompatibleTextProvider(settings)
