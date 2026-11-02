"""Apply one bounded frozen context per operation without changing provider signatures."""

from dataclasses import dataclass

from arclight.agents.contracts import ScriptContent, StoryboardContent
from arclight.agents.script_rewrite import RewriteResult
from arclight.agents.story_analysis import StoryAnalysisContent
from arclight.agents.story_outline import StoryOutlineContent
from arclight.domain.context import check_call
from arclight.providers.text import OpenAICompatibleTextProvider, TextProvider


@dataclass
class ContextTextProvider:
    provider: TextProvider
    contexts: dict[str, dict]

    def check(self, stage: str, value: dict) -> dict:
        return check_call(self.contexts[stage], value)["task_input"]

    def analyze(self, story: str) -> StoryAnalysisContent:
        self.check("analyze", {"story": story})
        return self.provider.analyze(story)

    def outline(self, source: dict) -> StoryOutlineContent:
        return self.provider.outline(self.check("outline", source))

    def rewrite(self, context: dict) -> RewriteResult:
        self.check("rewrite", context)
        return self.provider.rewrite(context)

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
        checked_input = self.check(
            "script",
            {
                "story": story,
                "style_guide": style,
                "target_duration_ms": duration_ms,
                "story_settings": story_settings,
                "story_context": story_context,
                "outline_context": outline_context,
            },
        )
        arguments = {}
        for name, value in (
            ("story_settings", story_settings),
            ("story_context", story_context),
            ("outline_context", outline_context),
        ):
            if value is not None:
                arguments[name] = value
        if "outline_context" in arguments:
            arguments["outline_context"] = checked_input["outline_context"]
        return self.provider.script(story, style, duration_ms, **arguments)

    def storyboard(self, script: ScriptContent, duration_ms: int) -> StoryboardContent:
        self.check(
            "storyboard",
            {"script": script.model_dump(mode="json"), "target_duration_ms": duration_ms},
        )
        return self.provider.storyboard(script, duration_ms)


def bind_contexts(provider: TextProvider, contexts: dict[str, dict]) -> TextProvider:
    if not contexts:
        return provider
    if isinstance(provider, OpenAICompatibleTextProvider):
        provider = OpenAICompatibleTextProvider(provider.settings, contexts)
    return ContextTextProvider(provider, contexts)
