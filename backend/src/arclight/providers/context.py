"""Apply one bounded frozen context per operation without changing provider signatures."""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from arclight.agents.contracts import ScriptContent, StoryboardContent
from arclight.agents.script_rewrite import RewriteResult
from arclight.agents.story_analysis import StoryAnalysisContent
from arclight.agents.story_outline import StoryOutlineContent
from arclight.core.errors import DomainError
from arclight.domain.context import check_call
from arclight.domain.generation_requests import safe_metadata
from arclight.providers.text import OpenAICompatibleTextProvider, TextProvider


def provider_call[T](
    call: Callable[[], T], *, provider=None, stage: str = "", data: dict | None = None
) -> T:
    # LangGraph persists exception strings in pending writes. Scrub transport
    # failures before they cross that boundary, rather than only in API logging.
    try:
        from arclight.application.gateway_text import invoke_text

        result = invoke_text(provider, stage, data or {}, call) if stage else call()
        if isinstance(result, BaseModel):
            safe_metadata(result.model_dump(mode="json"))
        return result
    except DomainError as exc:
        if exc.code.startswith(("GATEWAY_", "BUDGET_")) or exc.code in {
            "SERVICE_BALANCE_INSUFFICIENT",
            "SERVICE_AUTHENTICATION_FAILED",
            "SERVICE_RATE_LIMITED",
            "SERVICE_QUOTA_EXCEEDED",
        }:
            raise
        raise DomainError("TEXT_PROVIDER_FAILED", "文本模型调用失败", 502) from None
    except Exception:
        raise DomainError("TEXT_PROVIDER_FAILED", "文本模型调用失败", 502) from None


@dataclass
class ContextTextProvider:
    provider: TextProvider
    contexts: dict[str, dict]

    def check(self, stage: str, value: dict) -> dict:
        if stage not in self.contexts:
            return value
        return check_call(self.contexts[stage], value)["task_input"]

    def analyze(self, story: str) -> StoryAnalysisContent:
        self.check("analyze", {"story": story})
        return provider_call(
            lambda: self.provider.analyze(story),
            provider=self.provider,
            stage="analyze",
            data={"story": story},
        )

    def outline(self, source: dict) -> StoryOutlineContent:
        checked = self.check("outline", source)
        return provider_call(
            lambda: self.provider.outline(checked),
            provider=self.provider,
            stage="outline",
            data=checked,
        )

    def rewrite(self, context: dict) -> RewriteResult:
        self.check("rewrite", context)
        return provider_call(
            lambda: self.provider.rewrite(context),
            provider=self.provider,
            stage="rewrite",
            data=context,
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
        return provider_call(
            lambda: self.provider.script(story, style, duration_ms, **arguments),
            provider=self.provider,
            stage="script",
            data=checked_input,
        )

    def storyboard(self, script: ScriptContent, duration_ms: int) -> StoryboardContent:
        self.check(
            "storyboard",
            {"script": script.model_dump(mode="json"), "target_duration_ms": duration_ms},
        )
        return provider_call(
            lambda: self.provider.storyboard(script, duration_ms),
            provider=self.provider,
            stage="storyboard",
            data={"script": script.model_dump(mode="json"), "duration_ms": duration_ms},
        )


def bind_contexts(provider: TextProvider, contexts: dict[str, dict]) -> TextProvider:
    if isinstance(provider, OpenAICompatibleTextProvider):
        provider = OpenAICompatibleTextProvider(provider.settings, contexts)
    return ContextTextProvider(provider, contexts)
