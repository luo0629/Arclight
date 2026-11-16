"""Offline model HTTP for the real Producer queue, tools and durable loops."""

import copy
import json

import httpx


def respond(payload):
    from e2e_screenwriter import _completion

    seed = json.loads(payload["messages"][1]["content"])["data"]
    feedback = [
        json.loads(item["content"]) for item in payload["messages"] if item["role"] == "tool"
    ]
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
                                    "id": "producer-state",
                                    "type": "function",
                                    "function": {
                                        "name": "investigate_project",
                                        "arguments": json.dumps({"query": {"path": "overview"}}),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"},
            },
        )
    answered = any(
        item["role"] == "user" and "human_decision" in item.get("content", "")
        for item in payload["messages"]
    )
    if "需要澄清" in seed["goal"] and not answered:
        return httpx.Response(
            200,
            json=_completion(
                {
                    "decision": "HUMAN_REVIEW",
                    "result": None,
                    "human_request": {
                        "reason": "USER_DECISION",
                        "question": "先制作本集剧本吗？",
                        "options": ["先制作剧本", "暂停规划"],
                    },
                }
            ),
        )
    plan = {
        "schema_version": "producer-plan/1",
        "goal_sha256": seed["goal_sha256"],
        "summary": seed["goal"],
        "evidence": [
            {
                "tool_call_key": item["idempotency_key"],
                "evidence_sha256": item["result"]["evidence_sha256"],
            }
            for item in feedback
            if item["status"] == "SUCCEEDED"
        ],
        "tasks": copy.deepcopy(seed["base_plan"]["plan"]["tasks"])
        if "base_plan" in seed
        else [
            {
                "task_key": "write",
                "depends_on": [],
                "reason": "先制作可审核的本集剧本。",
                "task": {
                    "kind": "screenwriter",
                    "story": "雨夜，主角寻找纸船的失主。",
                    "revision_rounds": 1,
                },
            }
        ],
    }
    result = plan
    if "base_plan" in seed:
        result = {
            "schema_version": "producer-replan/1",
            "base_plan_id": seed["base_plan"]["plan_id"],
            "base_plan_sha256": seed["base_plan"]["plan_sha256"],
            "observation_id": seed["observation"]["id"],
            "observation_sha256": seed["observation"]["snapshot_sha256"],
            "reason": seed["observation"]["reason"],
            "plan": plan,
        }
    return httpx.Response(
        200,
        json=_completion(
            {
                "decision": "COMPLETE",
                "result": result,
                "human_request": None,
            }
        ),
    )
