"""Only model HTTP fixtures; actual director Runtime, tools, candidate and reviews execute."""

import json

import httpx

from arclight.agents.contracts import ScriptContent
from arclight.providers.text import MockTextProvider


def respond(payload):
    from e2e_screenwriter import _completion

    seed = json.loads(payload["messages"][1]["content"])["data"]
    if seed["phase"] == "CHECK_DIRECTOR":
        if any(
            tool["function"]["name"] == "request_art_opinion" for tool in payload.get("tools", [])
        ):
            from e2e_director_collaboration import respond as revise
        else:
            from e2e_director_revision import respond as revise

        return httpx.Response(200, json=revise(seed, payload))
    answered = any("human_decision" in str(item.get("content")) for item in payload["messages"])
    if seed["phase"] == "PLANNING_ONLY":
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
                                        "id": f"director-research-{i}",
                                        "type": "function",
                                        "function": {
                                            "name": "investigate_production",
                                            "arguments": json.dumps({"query": query}),
                                        },
                                    }
                                    for i, query in enumerate(
                                        [
                                            {"path": "script"},
                                            {"path": "visual"},
                                            {
                                                "path": "capabilities",
                                                "provider": "mock",
                                                "model": "mock-image",
                                            },
                                        ]
                                    )
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"},
                },
            )
        task = seed["constraints"]
        count = len(task["scene_ids"])
        duration, remainder = divmod(task["target_duration_ms"], count)
        result = {
            "schema_version": "director-plan/1",
            "objective": "按场景目标规划纸船归途的镜头",
            "script_version_id": task["script_version_id"],
            "script_sha256": task["script_sha256"],
            "target_duration_ms": task["target_duration_ms"],
            "estimated_duration_ms": task["target_duration_ms"],
            "scenes": [
                {
                    "scene_id": identity,
                    "dramatic_goal": "发现线索并决定行动",
                    "emotion": "期待转为坚定",
                    "staging": ["主角沿河岸行走，停下观察"],
                    "rhythm": ["先观察再行动"],
                    "estimated_duration_ms": duration + (remainder if index == 0 else 0),
                }
                for index, identity in enumerate(task["scene_ids"])
            ],
            "evidence": [
                {key: value for key, value in material.items() if key != "content"}
                for item in feedback
                if item["status"] == "SUCCEEDED"
                for material in item["result"]["materials"]
            ],
            "open_questions": [],
        }
    else:
        scene = seed["scene"]
        if "导演澄清" in scene["action"] and not answered:
            return httpx.Response(
                200,
                json=_completion(
                    {
                        "decision": "HUMAN_REVIEW",
                        "result": None,
                        "human_request": {
                            "reason": "USER_DECISION",
                            "question": "这一场先观察还是先行动？",
                            "options": ["先观察", "先行动"],
                        },
                    }
                ),
            )
        duration = seed["plan"]["scenes"][scene["scene_no"] - 1]["estimated_duration_ms"]
        source = ScriptContent(
            title="当前场景",
            summary="精确批准稿",
            characters=seed["characters"],
            character_assets=seed["character_assets"],
            estimated_duration_ms=duration,
            scenes=[scene | {"scene_no": 1}],
        )
        shots = MockTextProvider().storyboard(source, duration).model_dump(mode="json")["shots"]
        for shot in shots:
            shot["scene_no"] = scene["scene_no"]
        result = {
            "schema_version": "director-fragment/1",
            "plan_loop_id": seed["plan_loop_id"],
            "scene_id": scene["scene_id"],
            "scene_no": scene["scene_no"],
            "shots": shots,
            "evidence": seed["plan"]["evidence"],
        }
        if seed["phase"] == "DIRECT_WORKING_SCENE":
            result["schema_version"] = "director-working/1"
            if "导演检查遗漏" in scene.get("notes", ""):
                result["shots"][0]["dialogue_line_ids"] = []
    return httpx.Response(
        200, json=_completion({"decision": "COMPLETE", "result": result, "human_request": None})
    )
