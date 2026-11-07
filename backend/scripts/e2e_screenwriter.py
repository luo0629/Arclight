"""Test-server-only HTTP fixtures for the actual professional Runtime and review graph."""

import copy
import json
from uuid import uuid4

import httpx
from pydantic import SecretStr

from arclight.agents.contracts import ScriptContent
from arclight.application import production_versions
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.director_creation import DIRECTOR_VERSIONS
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.screenwriter_creation import SCREENWRITER_OUTLINE_VERSION, SCREENWRITER_VERSION
from arclight.domain.screenwriter_revision import CHECKS, PATCH_TOOL, REVISION_WORKFLOWS
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
    result = {
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
                                        "name": PATCH_TOOL,
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


def _respond(request):
    payload = json.loads(request.content)
    data = json.loads(payload["messages"][1]["content"])
    if "data" not in data:
        data = data.get("task_input", data)
        script = ScriptContent.model_validate(data["script"])
        return httpx.Response(
            200,
            json=_completion(
                text.MockTextProvider()
                .storyboard(script, data["target_duration_ms"])
                .model_dump(mode="json")
            ),
        )
    seed = data["data"]
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
        } | REVISION_WORKFLOWS | DIRECTOR_VERSIONS | {"art-proposal/1.0", "art-proposal/2.0"}:
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
                        max_input_images=4 if run.workflow_type == "art_proposal" else 0,
                        response_formats=frozenset({"json_schema", "json_object"}),
                    ),
                    "model_gateway_policy": GatewayPolicy(
                        text_maximum_cost="0", pricing_version="offline:1"
                    ),
                }
            )
        return original_capture(db, project_id, run, settings, actor=actor)

    production_versions.capture_text = capture
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
