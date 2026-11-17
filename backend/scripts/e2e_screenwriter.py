"""Test-server-only HTTP fixtures for the actual professional Runtime and review graph."""

import copy
import json
from uuid import uuid4

import httpx
from pydantic import SecretStr

from arclight.agents.contracts import read_script_content
from arclight.application import production_versions
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.director_creation import DIRECTOR_VERSIONS
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.screenwriter_creation import SCREENWRITER_OUTLINE_VERSION, SCREENWRITER_VERSION
from arclight.domain.screenwriter_revision import (
    CHECKS,
    PATCH_TOOL,
    REVISION_WORKFLOWS,
    SERIES_PATCH_TOOL,
)
from arclight.providers import agent_model, text


def _completion(value):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)},
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"},
    }


def _critique(seed, payload):
    """Only replace model HTTP; real tool execution, receipts and persistence remain active."""
    source = seed["source_script"]
    target = seed["permitted_target"] or {
        "scope": "scene",
        "scene_id": source["scenes"][0]["scene_id"],
    }
    selected = target.get("scene_ids", [target.get("scene_id")])
    scene = next(item for item in source["scenes"] if item["scene_id"] in selected)
    issue = {
        "category": "MOTIVATION",
        "scene_id": scene["scene_id"],
        "line_id": None,
        "problem": "寻找失主的动机需要说明",
        "evidence": scene["action"],
        "suggestion": "补充失主的嘱托",
    }
    series = source.get("schema_version") == "3.0"
    result = {
        "schema_version": "screenwriter-revision/2" if series else "screenwriter-revision/1",
        "source_sha256": seed["source_sha256"],
        "checks": sorted(CHECKS),
        "issues": [],
        "decision": "ACCEPT",
    }
    answered = any("human_decision" in str(item.get("content")) for item in payload["messages"])
    needs_patch = seed["round"] == 0
    if not answered and (
        needs_patch
        and seed["remaining_patches"] == 0
        or "修订耗尽" in seed["objective"]
        and seed["round"] >= 1
    ):
        result |= {"decision": "ESCALATE", "issues": [issue]}
    elif needs_patch and not answered:
        feedback = [
            json.loads(item["content"]) for item in payload["messages"] if item["role"] == "tool"
        ]
        if (
            feedback
            and feedback[-1]["status"] == "SUCCEEDED"
            and feedback[-1]["result"]["accepted"]
        ):
            result |= {
                "decision": "REVISE",
                "issues": [issue],
                "patch_sha256": feedback[-1]["result"]["patch_sha256"],
            }
        else:
            patch = {
                "estimated_duration_ms": source["estimated_duration_ms"],
                "scenes": [],
                "text": None,
            }
            if target["scope"] == "paragraph":
                patch["text"] = "因为记得失主的嘱托🙂，他决定送信。"
            else:
                patch["scenes"] = [
                    {
                        key: copy.deepcopy(row[key])
                        for key in ("scene_id", "title", "action", "dialogue")
                    }
                    for row in source["scenes"]
                    if row["scene_id"] in selected
                ]
                for row in patch["scenes"]:
                    row["action"] += "因为记得失主的嘱托，他决定送信。"
            if series:
                patch |= {
                    "schema_version": "series-rewrite/1",
                    "narrative_updates": [
                        {
                            key: copy.deepcopy(row[key])
                            for key in ("scene_id", "dramatic_beat", "state_changes")
                        }
                        for row in source["scenes"]
                        if row["scene_id"] in selected
                    ],
                }
            arguments = {
                "operation_key": seed["operation_key"],
                "source_sha256": seed["source_sha256"],
                "target": target,
                "instructions": seed["instructions"] or {"instruction": "补充动机"},
                "patch": patch,
            }
            return {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "check-patch",
                                    "type": "function",
                                    "function": {
                                        "name": SERIES_PATCH_TOOL if series else PATCH_TOOL,
                                        "arguments": json.dumps(arguments, ensure_ascii=False),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"},
            }
    return _completion({"decision": "COMPLETE", "result": result, "human_request": None})


def _series_response(seed, payload):
    """Exercise real narrative tools and consume their bounded, filtered results."""
    if seed["phase"] == "PLANNING_ONLY":
        context = seed["narrative_context"]
        feedback = [
            json.loads(row["content"]) for row in payload["messages"] if row["role"] == "tool"
        ]
        if not feedback:
            queries = [("author", {})] + [
                (f"character-{index}", {"character_id": identity})
                for index, identity in enumerate(context["character_ids"])
            ]
            return {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": identity,
                                    "type": "function",
                                    "function": {
                                        "name": "investigate_series_story",
                                        "arguments": json.dumps(parameters),
                                    },
                                }
                                for identity, parameters in queries
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"},
            }
        materials = [
            (item["result"]["manifest_id"], material)
            for item in feedback
            if item["status"] == "SUCCEEDED"
            for material in item["result"]["materials"]
        ]
        evidence = [
            {
                "manifest_id": manifest_id,
                **{
                    key: value
                    for key, value in material.items()
                    if key not in {"source", "content"}
                },
            }
            for manifest_id, material in materials
        ]
        author = next(
            material["content"]
            for _, material in materials
            if material["content"]["perspective"] == "AUTHOR"
        )
        constraints = seed["constraints"]
        common = {
            "story_time": context["target_story_time"],
            "character_ids": context["character_ids"],
            "goal": "从冻结事实推进人物目的与冲突",
            "beats": ["询问线索", "选择下一步行动"],
        }
        if constraints["mode"] == "rewrite":
            target = constraints["target"]
            identities = target.get("scene_ids", [target.get("scene_id")])
            plan = {
                "mode": "rewrite",
                "base_version_id": constraints["base_version_id"],
                "target": target,
                "estimated_duration_ms": constraints["target_duration_ms"],
                "scenes": [{"scene_id": identity, **common} for identity in identities],
            }
        else:
            duration = constraints["target_duration_ms"]
            plan = {
                "mode": "episode",
                "target_duration_ms": duration,
                "estimated_duration_ms": duration,
                "scenes": [
                    {
                        "scene_id": str(uuid4()),
                        "title": f"因果回响{index}",
                        **common,
                        "estimated_duration_ms": duration // 2
                        + (duration % 2 if index == 1 else 0),
                    }
                    for index in (1, 2)
                ],
            }
        causal = author["causal_summaries"]
        objective = "沿冻结全剧规划推进人物冲突"
        if "series03-distant-cause" in json.dumps(payload, ensure_ascii=False):
            objective = "series03-distant-cause：" + (
                f"引用第{causal[0]['episode_no']}集因果：{causal[0]['summary'][:120]}"
                if causal
                else "没有已批准远集来源，保留证据缺口"
            )
        result = {
            "schema_version": "screenwriter-plan/2",
            "narrative_context": context,
            "objective": objective,
            "goals": ["区分作者事实与逐角色所知"],
            "plan": plan,
            "evidence": evidence,
            "open_questions": [],
        }
    else:
        plan = seed["plan"]
        context = plan["narrative_context"]
        scene = plan["plan"]["scenes"][seed["scene_no"] - 1]
        views = seed["narrative_projections"]
        dialogue = []
        objectives = []
        for identity in scene["character_ids"]:
            view = next(row for row in views if row["character_id"] == identity)
            known = view["facts"]
            dialogue.append(
                {
                    "line_id": str(uuid4()),
                    "speaker": view["character_name"],
                    "text": f"我知道：{known[0]['value'][:120]}"
                    if known
                    else "我还不知道线索，需要询问。",
                }
            )
            objectives.append(
                {
                    "character_id": identity,
                    "purpose": "根据自己所知确认下一步行动",
                    "emotion_start": "疑惑",
                    "emotion_end": "决心",
                }
            )
        result = {
            "schema_version": "screenwriter-fragment/2",
            "narrative_context": context,
            "plan_loop_id": seed["plan_loop_id"],
            "scene": {
                "scene_id": scene["scene_id"],
                "scene_no": seed["scene_no"],
                "title": scene["title"],
                "action": plan["objective"] + "。人物交换已知线索后作出选择。",
                "dialogue": dialogue,
                "dramatic_beat": {
                    "story_time": scene["story_time"],
                    "character_objectives": objectives,
                    "conflict": "双方掌握不同信息，必须先询问再决定",
                    "dialogue_interactions": [
                        {
                            "line_ids": [row["line_id"] for row in dialogue],
                            "character_ids": scene["character_ids"],
                            "action": "询问、倾听并依据已知线索回应",
                        }
                    ],
                },
                "state_changes": [
                    {
                        "id": str(uuid4()),
                        "kind": "EVENT",
                        "state_key": f"series03_scene_{seed['scene_no']}",
                        "value": "series03-distant-cause：人物决定调查线索",
                        "story_start": scene["story_time"],
                    }
                ],
            },
            "characters": [row["speaker"] for row in dialogue],
            "character_assets": [],
            "estimated_duration_ms": scene["estimated_duration_ms"],
            "evidence": plan["evidence"],
        }
    return _completion({"decision": "COMPLETE", "result": result, "human_request": None})


def _respond(request):
    payload = json.loads(request.content)
    data = json.loads(payload["messages"][1]["content"])
    if "data" not in data:
        data = data.get("task_input", data)
        script = read_script_content(data["script"])
        return httpx.Response(
            200,
            json=_completion(
                text.MockTextProvider()
                .storyboard(script, data["target_duration_ms"])
                .model_dump(mode="json")
            ),
        )
    seed = data["data"]
    if "goal_sha256" in seed:
        from e2e_producer import respond

        return respond(payload)
    if seed.get("schema_version") == "art-direction/1":
        from e2e_art import respond

        return respond(payload)
    if seed["phase"] in {
        "DIRECT_SCENE_ONLY",
        "DIRECT_WORKING_SCENE",
        "CHECK_DIRECTOR",
    } or "script_version_id" in seed.get("constraints", {}):
        from e2e_director import respond

        return respond(payload)
    if seed["phase"] == "CRITIQUE_SCRIPT":
        return httpx.Response(200, json=_critique(seed, payload))
    if "narrative_context" in seed or "narrative_context" in seed.get("plan", {}):
        return httpx.Response(200, json=_series_response(seed, payload))
    if seed["phase"] == "PLANNING_ONLY":
        feedback = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        if not feedback:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "research",
                                        "type": "function",
                                        "function": {
                                            "name": "investigate_story",
                                            "arguments": json.dumps({"query": {"path": "startup"}}),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"},
                },
            )
        evidence = [
            {
                "manifest_id": item["result"]["manifest_id"],
                **{k: v for k, v in material.items() if k not in {"source", "content"}},
            }
            for item in feedback
            if item["status"] == "SUCCEEDED"
            for material in item["result"]["materials"]
        ]
        constraints = seed["constraints"]
        if constraints["mode"] == "season":
            plan = {
                "mode": "season",
                "outline": text.MockTextProvider()
                .outline({"story": "离线纸船故事", **constraints})
                .model_dump(mode="json"),
            }
        elif constraints["mode"] == "rewrite":
            target = constraints["target"]
            plan = {
                "mode": "rewrite",
                "base_version_id": constraints["base_version_id"],
                "target": target,
                "estimated_duration_ms": constraints["target_duration_ms"],
                "scenes": [
                    {"scene_id": identity, "goal": "按用户范围补充动机", "beats": ["保留前后文"]}
                    for identity in target.get("scene_ids", [target.get("scene_id")])
                ],
            }
        else:
            duration = constraints["target_duration_ms"]
            plan = {
                "mode": "episode",
                "target_duration_ms": duration,
                "estimated_duration_ms": duration,
                "scenes": [
                    {
                        "scene_id": str(uuid4()),
                        "title": f"河边{i}",
                        "goal": "寻找纸船失主",
                        "beats": ["发现线索", "决定行动"],
                        "estimated_duration_ms": duration // 2 + (duration % 2 if i == 1 else 0),
                    }
                    for i in (1, 2)
                ],
            }
        result = {
            "schema_version": "screenwriter-plan/1",
            "objective": "修订耗尽需人工决定"
            if "修订耗尽" in json.dumps(payload, ensure_ascii=False)
            else "沿冻结故事规划纸船的归途",
            "goals": ["保持动机与设定一致"],
            "plan": plan,
            "evidence": evidence,
            "open_questions": [],
        }
    else:
        answered = any(
            m["role"] == "user" and "human_decision" in str(m.get("content"))
            for m in payload["messages"]
        )
        if (
            seed["scene_no"] == 2
            and "需要澄清" in json.dumps(payload, ensure_ascii=False)
            and not answered
        ):
            return httpx.Response(
                200,
                json=_completion(
                    {
                        "decision": "HUMAN_REVIEW",
                        "result": None,
                        "human_request": {
                            "reason": "USER_DECISION",
                            "question": "纸船是否送回失主？",
                            "options": ["送回失主", "保留纸船"],
                        },
                    }
                ),
            )
        scene = seed["plan"]["plan"]["scenes"][seed["scene_no"] - 1]
        result = {
            "schema_version": "screenwriter-fragment/1",
            "plan_loop_id": seed["plan_loop_id"],
            "scene": {
                "scene_id": scene["scene_id"],
                "scene_no": seed["scene_no"],
                "title": scene["title"],
                "action": "主角循着河岸的线索寻找失主。",
                "dialogue": [{"line_id": str(uuid4()), "speaker": "主角", "text": "把信送回去。"}],
            },
            "characters": ["主角"],
            "character_assets": [],
            "estimated_duration_ms": scene["estimated_duration_ms"],
            "evidence": seed["plan"]["evidence"],
        }
    return httpx.Response(
        200, json=_completion({"decision": "COMPLETE", "result": result, "human_request": None})
    )


def install():
    original_capture = production_versions.capture_text

    def capture(db, project_id, run, settings, *, actor=None):
        if run.workflow_version in {
            SCREENWRITER_VERSION,
            SCREENWRITER_OUTLINE_VERSION,
        } | REVISION_WORKFLOWS | DIRECTOR_VERSIONS | {
            "art-proposal/1.0",
            "art-proposal/2.0",
            "producer/1",
        }:
            settings = settings.model_copy(
                update={
                    "provider": "openai_compatible",
                    "llm_model": "offline-professional",
                    "llm_supplier": "custom",
                    "llm_base_url": "https://screenwriter.invalid/v1",
                    "llm_api_key": SecretStr("offline-only"),
                    "llm_agent_capabilities": AgentModelCapabilities(
                        version="e2e:1",
                        supplier="custom",
                        model="offline-professional",
                        tools=True,
                        max_input_images=4
                        if run.workflow_type == "art_proposal"
                        or run.input_json.get("director", {}).get("art_collaboration")
                        else 0,
                        response_formats=frozenset({"json_schema", "json_object"}),
                    ),
                    "model_gateway_policy": GatewayPolicy(
                        text_maximum_cost="0", pricing_version="offline:1"
                    ),
                }
            )
        return original_capture(db, project_id, run, settings, actor=actor)

    production_versions.capture_text = capture
    # Producer deliberately imported the capture function for its trusted application entry.
    from arclight.application import producer_planning

    producer_planning.capture_text = capture
    for module in (agent_model, text):
        original_transport = module.PublicHTTPTransport

        class Transport(httpx.BaseTransport):
            def __init__(self, *args, original=original_transport, **kwargs):
                self.real = original(*args, **kwargs)

            def handle_request(self, request):
                return (
                    _respond(request)
                    if request.url.host == "screenwriter.invalid"
                    else self.real.handle_request(request)
                )

            def close(self):
                self.real.close()

        module.PublicHTTPTransport = Transport
