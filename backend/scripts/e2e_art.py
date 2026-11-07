"""Offline HTTP fixture; real investigation, comparison, Runtime and approvals still run."""

import json
from uuid import UUID, uuid5

import httpx


def completion(value):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(value, ensure_ascii=False),
                },
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"},
    }


def calls(values):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": f"{name}_{index}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                        for index, (name, arguments) in enumerate(values)
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"},
    }


def draft(seed, feedback):
    namespace = UUID(seed["script_version_id"])

    def identity(label):
        return str(uuid5(namespace, label))

    materials = [
        material
        for value in feedback
        if value["tool_name"] == "investigate_art" and value["status"] == "SUCCEEDED"
        for material in value["result"]["materials"]
    ]
    sources = [
        {
            "kind": "VERSION",
            "source_id": identity("script"),
            "resource_type": "SCRIPT",
            "resource_id": seed["script_version_id"],
            "content_sha256": seed["script_sha256"],
        }
    ]
    if seed["visual_bible_source"]:
        sources.append(seed["visual_bible_source"])
    source_ids = [source["source_id"] for source in sources]
    options = [
        {
            "option_id": identity(f"option-{i}"),
            "title": title,
            "rationale": "依据已确认剧本与完整规范，保留角色辨识度。",
            "designs": [
                {
                    "design_id": identity(f"design-{i}"),
                    "kind": "CHARACTER",
                    "target": {"mode": "NEW"},
                    "name": title + "主角",
                    "metadata": {"description": "雨夜纸船的主角，外观保持朴素"},
                }
            ],
            "source_ids": source_ids,
            "prompts": [
                {
                    "fragment_id": identity(f"prompt-{i}"),
                    "kind": "POSITIVE",
                    "text": title + "，朴素服饰，纸张纹理，清晰轮廓。",
                    "design_ids": [identity(f"design-{i}")],
                    "source_ids": source_ids,
                }
            ],
        }
        for i, title in enumerate(("蓝灰雨夜", "暖红轮廓"))
    ]
    return {
        "proposal": {
            "schema_version": "art-proposal/1",
            "project_id": seed["project_id"],
            "episode_id": seed["episode_id"],
            "origin": "AGENT",
            "visual_goal": seed["task"]["visual_goal"],
            "constraints": seed["task"]["constraints"],
            "sources": sources,
            "options": options,
            "preferred_option_id": options[0]["option_id"],
        },
        "evidence": [
            {k: v for k, v in material.items() if k != "content"} for material in materials
        ],
        "assessments": [
            {
                "option_id": option["option_id"],
                "strengths": "以颜色和轮廓区分叙事情绪",
                "tradeoffs": "控制背景饱和度",
                "evidence_manifest_ids": list(dict.fromkeys(m["manifest_id"] for m in materials)),
                "reference_ids": [],
            }
            for option in options
        ],
    }


def respond(payload):
    seed = json.loads(payload["messages"][1]["content"])["data"]
    if "失败" in seed["task"]["visual_goal"]:
        return httpx.Response(401, json={"error": {"message": "Offline fixture rejected"}})
    feedback = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
    if not feedback:
        value = calls(
            [
                ("investigate_art", {"query": {"path": "visual"}}),
                ("investigate_art", {"query": {"path": "model"}}),
            ]
        )
    elif (
        "澄清" in seed["task"]["visual_goal"]
        and not any(
            m["role"] == "user" and "human_decision" in str(m.get("content"))
            for m in payload["messages"]
        )
    ) or seed["task"]["minimum_reference_images"] > 0:
        value = completion(
            {
                "decision": "HUMAN_REVIEW",
                "result": None,
                "human_request": {
                    "reason": "USER_DECISION",
                    "question": "美术方案采用冷色还是暖色？",
                    "options": ["冷色", "暖色"],
                },
            }
        )
    else:
        comparisons = [f for f in feedback if f["tool_name"] == "compare_art_options"]
        original = draft(seed, feedback)
        if not comparisons:
            value = calls([("compare_art_options", {"draft": original})])
        elif comparisons[-1]["status"] != "SUCCEEDED":
            raise RuntimeError("Offline art comparison failed: " + str(comparisons[-1]))
        else:
            value = completion(
                {
                    "decision": "COMPLETE",
                    "human_request": None,
                    "result": {
                        "draft": original,
                        "comparison_sha256": comparisons[-1]["result"]["comparison"][
                            "comparison_sha256"
                        ],
                    },
                }
            )
    return httpx.Response(200, json=value)
