"""Resolve exact selections and merge only fields the user authorized."""

import hashlib
import json

from arclight.agents.contracts import Dialogue, Scene, ScriptContent
from arclight.agents.script_rewrite import (
    ChapterTarget,
    ParagraphTarget,
    RewriteInstructions,
    RewriteRequest,
    RewriteResult,
    RewriteTarget,
)
from arclight.core.errors import DomainError


def content_hash(content: dict) -> str:
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def selected_scenes(source: ScriptContent, target: RewriteTarget) -> list[Scene]:
    ids = target.scene_ids if isinstance(target, ChapterTarget) else [target.scene_id]
    positions = {scene.scene_id: i for i, scene in enumerate(source.scenes)}
    indexes = [positions.get(identity, -1) for identity in ids]
    if -1 in indexes or indexes != list(range(indexes[0], indexes[0] + len(indexes))):
        raise DomainError("INVALID_REWRITE_TARGET", "请选择本版本中连续且不重复的场景", 422)
    return [source.scenes[i] for i in indexes]


def paragraph_field(scene: Scene, target: ParagraphTarget) -> Scene | Dialogue:
    if target.field == "action":
        return scene
    line = next((line for line in scene.dialogue if line.line_id == target.line_id), None)
    if line is None:
        raise DomainError("INVALID_REWRITE_TARGET", "台词不属于选定场景", 422)
    return line


def paragraph_text(scene: Scene, target: ParagraphTarget) -> str:
    field = paragraph_field(scene, target)
    text = field.action if isinstance(field, Scene) else field.text
    if target.end > len(text) or not text[target.start : target.end].strip():
        raise DomainError("INVALID_REWRITE_TARGET", "选定段落为空或超出原文字数", 422)
    return text


def provider_context(source: ScriptContent, target: RewriteTarget) -> dict:
    scenes = selected_scenes(source, target)
    result: dict = {
        "schema_version": "1.0",
        "title": source.title,
        "summary": source.summary,
        "characters": source.characters,
        "estimated_duration_ms": source.estimated_duration_ms,
        "target": target.model_dump(mode="json"),
    }
    if isinstance(target, ParagraphTarget):
        text = paragraph_text(scenes[0], target)
        result["text"] = text[target.start : target.end]
        result["surrounding_text"] = {
            "before": text[: target.start],
            "after": text[target.end :],
        }
        field = paragraph_field(scenes[0], target)
        if isinstance(field, Dialogue):
            result["line_context"] = {"speaker": field.speaker, "line_type": field.line_type}
    else:
        result["scenes"] = [
            {
                "scene_id": scene.scene_id,
                "title": scene.title,
                "action": scene.action,
                "dialogue": [line.model_dump() for line in scene.dialogue],
            }
            for scene in scenes
        ]
    return result


def merge_rewrite(inputs: dict, result: RewriteResult) -> ScriptContent:
    source = ScriptContent.model_validate(inputs["source_script"])
    request = RewriteRequest.model_validate(inputs["request"])
    target = request.target
    scenes = selected_scenes(source, target)
    merged = source.model_copy(deep=True)
    if isinstance(target, ParagraphTarget):
        if result.scenes or result.text is None or not result.text.strip():
            raise ValueError("段落改写只能返回非空 text")
        text = paragraph_text(scenes[0], target)
        scene = next(row for row in merged.scenes if row.scene_id == target.scene_id)
        field = paragraph_field(scene, target)
        replaced = text[: target.start] + result.text + text[target.end :]
        if isinstance(field, Scene):
            field.action = replaced
        else:
            field.text = replaced
        selected_lines = [field] if isinstance(field, Dialogue) else []
    else:
        if result.text is not None or [row.scene_id for row in result.scenes] != [
            row.scene_id for row in scenes
        ]:
            raise ValueError("改写必须精确返回选定场景，不能增加、删除或重排场景")
        original_ids = {line.line_id for scene in source.scenes for line in scene.dialogue}
        for patch in result.scenes:
            scene = next(row for row in merged.scenes if row.scene_id == patch.scene_id)
            own = {line.line_id: line for line in scene.dialogue}
            for line in patch.dialogue:
                if line.line_id in original_ids and line.line_id not in own:
                    raise ValueError("不能挪用其他场景的台词身份")
            scene.title, scene.action = patch.title, patch.action
            scene.dialogue = [Dialogue.model_validate(line.model_dump()) for line in patch.dialogue]
        selected_ids = {row.scene_id for row in scenes}
        selected_lines = [
            line
            for scene in merged.scenes
            if scene.scene_id in selected_ids
            for line in scene.dialogue
        ]
    check_instructions(request.instructions, selected_lines)
    merged.estimated_duration_ms = result.estimated_duration_ms
    # Validate after merging, including untouched references, length limits and global line IDs.
    return ScriptContent.model_validate(merged.model_dump())


def check_instructions(instructions: RewriteInstructions, selected: list[Dialogue]) -> None:
    if instructions.dialogue_max_chars is not None and any(
        len(line.text) > instructions.dialogue_max_chars
        for line in selected
        if line.line_type == "dialogue"
    ):
        raise ValueError("改写后的对白超过指定字数")
    total = sum(len(line.text) for line in selected)
    narration = sum(len(line.text) for line in selected if line.line_type == "narration")
    if instructions.narration_max_percent is not None and (
        narration * 100 > total * instructions.narration_max_percent
    ):
        raise ValueError("改写范围内旁白字数占比超过指定上限")
