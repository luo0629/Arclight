"""Deterministically compose image prompts from frozen visual inputs."""

import json
from typing import Any

from arclight.core.errors import DomainError
from arclight.domain.versions import digest

PROMPT_COMPOSER_VERSION = "1"
PROMPT_BLOCK_ORDER = (
    "project_style",
    "character_constraints",
    "location_constraints",
    "prop_constraints",
    "shot_description",
    "camera_language",
    "continuity_context",
    "negative_constraints",
)
PROMPT_COMPOSER_DEFINITION = {
    "blocks": list(PROMPT_BLOCK_ORDER),
    "asset_selection": "explicit exact generation inputs only",
    "style_selection": "latest project Visual Bible version frozen at capture",
    "source_order": ["asset_type", "asset_id", "asset_version_id"],
    "positive_rendering": "ordered labeled blocks separated by blank lines",
    "negative_order": [
        "visual_bible.prohibited",
        "visual_bible.negative_prompt",
        "request.negative_prompt",
    ],
    "provider_parameters": "stored separately from natural-language prompt",
}
BLOCK_LABELS = {
    "project_style": "项目风格",
    "character_constraints": "角色设定",
    "location_constraints": "场景设定",
    "prop_constraints": "道具设定",
    "shot_description": "画面描述",
    "camera_language": "镜头语言",
    "continuity_context": "连续性上下文",
    "negative_constraints": "负面约束",
}
ASSET_TYPE_ORDER = {"STYLE": 0, "CHARACTER": 1, "SCENE": 2, "PROP": 3}
APPEARANCE_LABELS = {
    "age_range": "年龄",
    "appearance": "外观",
    "hairstyle": "发型",
    "clothing": "服装",
    "build": "体态",
    "expressions": "表情",
    "colors": "颜色",
    "materials": "材质",
}


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _field_lines(values: dict, labels: dict[str, str]) -> list[str]:
    return [
        f"{label}：{value}" for key, label in labels.items() if (value := _text(values.get(key)))
    ]


def _metadata_lines(snapshot: dict) -> list[str]:
    metadata = snapshot.get("metadata")
    if not isinstance(metadata, dict):
        return []
    lines = []
    if description := _text(metadata.get("description")):
        lines.append(f"补充说明：{description}")
    appearance = metadata.get("appearance")
    if isinstance(appearance, dict):
        lines.extend(_field_lines(appearance, APPEARANCE_LABELS))
    return lines


def _asset_lines(asset_type: str, snapshot: dict) -> list[str]:
    lines = [f"名称：{_text(snapshot.get('name'))}"]
    lines.extend(_metadata_lines(snapshot))
    profile_key = {
        "CHARACTER": "character_profile",
        "SCENE": "scene_profile",
        "PROP": "prop_profile",
    }.get(asset_type)
    profile = snapshot.get(profile_key) if profile_key else None
    if not isinstance(profile, dict):
        return [line for line in lines if not line.endswith("：")]

    if asset_type == "CHARACTER":
        for key, label in (
            ("fixed_features", "固定特征"),
            ("variable_features", "可变特征"),
            ("expressions", "可用表情"),
        ):
            values = profile.get(key)
            if isinstance(values, list):
                content = "、".join(value for item in values if (value := _text(item)))
                if content:
                    lines.append(f"{label}：{content}")
        outfits = profile.get("outfits")
        if isinstance(outfits, list):
            default_id = profile.get("default_outfit_id")
            for outfit in outfits:
                if not isinstance(outfit, dict):
                    continue
                name = _text(outfit.get("name"))
                description = _text(outfit.get("description"))
                if name or description:
                    marker = "（默认）" if outfit.get("id") == default_id else ""
                    lines.append(f"服装{marker}：{name} {description}".strip())
    elif asset_type == "SCENE":
        lines.extend(
            _field_lines(
                profile,
                {
                    "layout": "空间布局",
                    "time_of_day": "时间",
                    "lighting": "光照",
                    "palette": "场景配色",
                    "environment": "环境",
                },
            )
        )
    elif asset_type == "PROP":
        lines.extend(
            _field_lines(
                profile,
                {
                    "appearance": "外观",
                    "story_state": "剧情状态",
                },
            )
        )
        holder = profile.get("holder")
        if isinstance(holder, dict):
            asset_id = _text(holder.get("asset_id"))
            version_id = _text(holder.get("asset_version_id"))
            if asset_id and version_id:
                lines.append(f"持有角色精确引用：{asset_id}/{version_id}")
    return [line for line in lines if not line.endswith("：")]


def _merge_text(values: list[str]) -> str:
    result = []
    seen = set()
    for item in values:
        value = _text(item)
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return "\n".join(result)


def compose_image_prompt(
    *,
    prompt: str,
    negative_prompt: str | None,
    context: dict,
    parameters: dict,
    output_spec: dict,
    seed: int | None,
    variation: str | None,
    batch_size: int,
    asset_sources: list[dict],
    visual_bible: dict,
) -> tuple[str, str, dict]:
    """Return composed positive/negative prompts and a reproducible manifest."""
    unique_assets: dict[str, dict] = {}
    asset_versions: dict[str, str] = {}
    for source in asset_sources:
        asset_id = source["asset_id"]
        version_id = source["asset_version_id"]
        previous = asset_versions.setdefault(asset_id, version_id)
        if previous != version_id:
            raise DomainError(
                "PROMPT_SOURCE_VERSION_CONFLICT",
                "同一资产在提示词组合中只能使用一个精确版本",
                409,
            )
        existing = unique_assets.get(version_id)
        if existing is None:
            unique_assets[version_id] = {
                **source,
                "dependency_types": sorted({source["dependency_type"]}),
                "selectors": sorted(set(source.get("selectors", []))),
            }
        else:
            existing["dependency_types"] = sorted(
                set(existing["dependency_types"]) | {source["dependency_type"]}
            )
            existing["selectors"] = sorted(
                set(existing["selectors"]) | set(source.get("selectors", []))
            )

    ordered_assets = sorted(
        unique_assets.values(),
        key=lambda item: (
            ASSET_TYPE_ORDER[item["asset_type"]],
            item["asset_id"],
            item["asset_version_id"],
        ),
    )
    source_records = [
        {
            key: item[key]
            for key in (
                "source_key",
                "source_type",
                "asset_type",
                "asset_id",
                "asset_version_id",
                "version_no",
                "checksum",
                "asset_snapshot_sha256",
                "dependency_types",
                "selectors",
            )
        }
        for item in ordered_assets
    ]
    visual_source = {
        key: visual_bible[key]
        for key in ("source_key", "source_type", "version_id", "version_no", "snapshot_sha256")
    }
    source_records.insert(0, visual_source)

    block_text = {key: "" for key in PROMPT_BLOCK_ORDER}
    block_sources: dict[str, list[str]] = {key: [] for key in PROMPT_BLOCK_ORDER}
    style_fields = visual_bible.get("fields", {})
    if not isinstance(style_fields, dict):
        style_fields = {}
    style_lines = _field_lines(
        style_fields,
        {
            "art_style": "画风",
            "line_style": "线条风格",
            "render_style": "渲染风格",
            "palette": "主要配色",
            "lighting": "光照原则",
            "composition": "构图偏好",
        },
    )
    style_sources = [visual_source["source_key"]]
    for source in ordered_assets:
        snapshot = source["asset_snapshot"]
        lines = _asset_lines(source["asset_type"], snapshot)
        if source["asset_type"] == "STYLE":
            style_lines.extend(lines)
            style_sources.append(source["source_key"])
        else:
            block_key = {
                "CHARACTER": "character_constraints",
                "SCENE": "location_constraints",
                "PROP": "prop_constraints",
            }[source["asset_type"]]
            block_text[block_key] = _merge_text([block_text[block_key], "\n".join(lines)])
            block_sources[block_key].append(source["source_key"])
    block_text["project_style"] = _merge_text(style_lines)
    block_sources["project_style"] = style_sources

    shot_lines = [f"画面要求：{_text(prompt)}"]
    if shot_description := _context_text(context, "shot_description"):
        shot_lines.append(f"补充画面描述：{shot_description}")
    block_text["shot_description"] = _merge_text(shot_lines)
    block_sources["shot_description"] = ["request.spec.prompt"]
    if context.get("shot_description"):
        block_sources["shot_description"].append("request.spec.context.shot_description")

    if camera := _context_text(context, "camera_language"):
        block_text["camera_language"] = camera
        block_sources["camera_language"] = ["request.spec.context.camera_language"]
    continuity = []
    if summary := _context_text(context, "summary"):
        continuity.append(f"剧情摘要：{summary}")
    if facts := _context_text(context, "continuity_context"):
        continuity.append(f"连续性要求：{facts}")
    block_text["continuity_context"] = _merge_text(continuity)
    if continuity:
        block_sources["continuity_context"] = [
            f"request.spec.context.{key}"
            for key in ("summary", "continuity_context")
            if _context_text(context, key)
        ]

    negative_values = [
        _text(style_fields.get("prohibited")),
        _text(style_fields.get("negative_prompt")),
        _text(negative_prompt),
    ]
    merged_negative = _merge_text(negative_values)
    block_text["negative_constraints"] = merged_negative
    block_sources["negative_constraints"] = [
        f"{visual_source['source_key']}.fields.{key}"
        for key in ("prohibited", "negative_prompt")
        if _text(style_fields.get(key))
    ]
    if _text(negative_prompt):
        block_sources["negative_constraints"].append("request.spec.negative_prompt")

    blocks = [
        {"key": key, "content": block_text[key], "source_keys": block_sources[key]}
        for key in PROMPT_BLOCK_ORDER
    ]
    rendered = [
        f"【{BLOCK_LABELS[key]}】\n{block_text[key]}"
        for key in PROMPT_BLOCK_ORDER
        if block_text[key] and key != "negative_constraints"
    ]
    merged_prompt = "\n\n".join(rendered)
    if len(merged_prompt) > 20000 or len(merged_negative) > 10000:
        raise DomainError("PROMPT_COMPOSITION_TOO_LARGE", "组合后的提示词超出长度限制", 422)

    manifest = {
        "schema_version": 1,
        "composer_version": PROMPT_COMPOSER_VERSION,
        "composition_basis": PROMPT_COMPOSER_DEFINITION,
        "sources": source_records,
        "blocks": blocks,
        "negative_prompt": merged_negative,
        "provider_parameters": {
            "parameters": parameters,
            "output_spec": output_spec,
            "seed": seed,
            "variation": variation,
            "batch_size": batch_size,
        },
        "prompt_sha256": digest(merged_prompt),
        "negative_prompt_sha256": digest(merged_negative),
    }
    if len(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()) > 65536:
        raise DomainError("PROMPT_COMPOSITION_TOO_LARGE", "提示词组合清单超出大小限制", 422)
    return merged_prompt, merged_negative, manifest


def _context_text(context: dict, key: str) -> str:
    value = context.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DomainError("INVALID_PROMPT_CONTEXT", f"提示词上下文 {key} 必须是文本", 422)
    return _text(value)
