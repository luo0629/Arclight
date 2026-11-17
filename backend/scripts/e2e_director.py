"""Only model HTTP fixtures; actual director Runtime, tools, candidate and reviews execute."""

import json

import httpx

from arclight.agents.contracts import ScriptContent
from arclight.providers.text import MockTextProvider


def respond(payload):
    from e2e_screenwriter import _completion

    seed = json.loads(payload["messages"][1]["content"])["data"]
    if (
        seed["phase"] == "CHECK_DIRECTOR"
        and seed.get("plan", {}).get("schema_version") == "director-performance-plan/1"
    ):
        return httpx.Response(200, json=_performance_revision(seed, payload))
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
        if seed.get("script", {}).get("schema_version") == "3.0":
            result["schema_version"] = "director-performance-plan/1"
            result["generation_routes"] = [{"provider": "mock", "model": "mock-image"}]
    else:
        if seed.get("plan", {}).get("schema_version") == "director-performance-plan/1":
            return httpx.Response(200, json=_performance_fragment(seed))
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


def _performance_fragment(seed):
    from e2e_screenwriter import _completion

    scene = seed["scene"]
    duration = seed["plan"]["scenes"][scene["scene_no"] - 1]["estimated_duration_ms"]
    count = max(2, (duration + 29999) // 30000)
    span, remainder = divmod(duration, count)
    shots = []
    for number in range(1, count + 1):
        lines = scene["dialogue"] if number == 1 else []
        people = [
            {
                "character": character,
                "objective": "回应当前剧情冲突",
                "emotion_start": "克制",
                "emotion_end": "警觉",
                "action_start": "停留",
                "action_end": "回应" if number == 1 else "停留",
                "position_start": character + "所在位置",
                "position_end": character + "所在位置",
                "eyeline_start": "场景中心",
                "eyeline_end": "场景中心",
            }
            for character in seed["characters"]
        ]
        interactions = (
            [
                {
                    "actor": people[0]["character"],
                    "target": people[1]["character"],
                    "action": "提出要求",
                    "reaction": "拒绝后等待回应",
                }
            ]
            if number == 1 and len(people) > 1
            else []
        )
        shots.append(
            {
                "schema_version": "3.0",
                "shot_no": number,
                "scene_no": scene["scene_no"],
                "scene_id": scene["scene_id"],
                "duration_ms": span + (remainder if number == 1 else 0),
                "visual_description": "演示表演工作稿（Mock）：冲突与人物反应，尚未真实制作验收",
                "characters": seed["characters"],
                "character_details": [
                    {"character": item["character"], "asset": item["asset"]}
                    for item in seed["character_assets"]
                ],
                "scene_asset": scene.get("scene_asset"),
                "location_asset": scene.get("location_asset"),
                "props": scene.get("props", []),
                "dialogue_line_ids": [line["line_id"] for line in lines],
                "performance": {
                    "scene_purpose": scene["dramatic_beat"]["conflict"],
                    "narrative_purpose": "呈现冲突" if number == 1 else "保留反应与停顿",
                    "emotion_rhythm": "要求、拒绝、停顿",
                    "characters": people,
                    "interactions": interactions,
                    "space_start": scene["title"],
                    "space_end": scene["title"],
                    "props": [
                        {
                            "asset": asset,
                            "holder_start": "",
                            "holder_end": "",
                            "position_start": "原位置",
                            "position_end": "原位置",
                        }
                        for asset in scene.get("props", [])
                    ],
                    "continuity_from_shot": number - 1 if number > 1 else None,
                    "motion_requirement": "dynamic_required" if number == 1 else "static_allowed",
                    "dialogue": [
                        {
                            "line_id": line["line_id"],
                            "speaker": line["speaker"],
                            "text": line["text"],
                            "presentation": "voice_over"
                            if line.get("line_type") == "narration"
                            else "visible",
                            "emotion": "克制",
                            "pacing": "短句后留半拍",
                            "lipsync": "not_applicable"
                            if line.get("line_type") == "narration"
                            else "required",
                            "exemption_reason": "旁白不出镜"
                            if line.get("line_type") == "narration"
                            else "",
                        }
                        for line in lines
                    ],
                },
            }
        )
    return _completion(
        {
            "decision": "COMPLETE",
            "human_request": None,
            "result": {
                "schema_version": "director-performance-working/1",
                "plan_loop_id": seed["plan_loop_id"],
                "scene_id": scene["scene_id"],
                "scene_no": scene["scene_no"],
                "shots": shots,
                "evidence": seed["plan"]["evidence"],
            },
        }
    )


def _performance_revision(seed, payload):
    from e2e_director_revision import _tool
    from e2e_screenwriter import _completion

    feedback = [
        json.loads(item["content"]) for item in payload["messages"] if item["role"] == "tool"
    ]
    if not feedback:
        return _tool(
            "check-performance-current",
            {
                "operation_key": seed["operation_key"],
                "source_sha256": seed["source_sha256"],
            },
        )
    checked = feedback[-1].get("result") or {}
    changed = checked.get("content_sha256") not in {None, seed["source_sha256"]}
    if checked.get("accepted") and (changed or checked.get("valid")):
        return _completion(
            {
                "decision": "COMPLETE",
                "human_request": None,
                "result": {
                    "schema_version": "director-performance-revision/1",
                    "source_sha256": seed["source_sha256"],
                    "check_sha256": checked["check_sha256"],
                    "decision": "REVISE" if changed else "ACCEPT",
                    "reason": "保留能力问题并记录局部人工处理说明"
                    if changed
                    else "真实检查通过后等待原人工审核",
                },
            }
        )
    scene = seed["source"]["scenes"][0]
    first = scene["shots"][0]
    if seed["remaining_patches"] > 0 and not first.get("notes"):
        import copy

        shot = copy.deepcopy(first)
        shot["notes"] = "能力不足保留动态/口型要求，等待人工决定，未声明已可生产"
        return _tool(
            "record-performance-blocker",
            {
                "operation_key": seed["operation_key"],
                "source_sha256": seed["source_sha256"],
                "patch": {"scene_id": scene["scene_id"], "start": 0, "end": 1, "shots": [shot]},
            },
        )
    return _completion(
        {
            "decision": "HUMAN_REVIEW",
            "result": None,
            "human_request": {
                "reason": "CONSTRAINT_CONFLICT",
                "question": (
                    "表演草稿已保留，但Mock/动作/口型能力未经真实验收；"
                    "请保留动态要求并修订或停止，不能静默转静图。"
                ),
                "options": ["停止并补齐真实能力", "保留草稿继续人工规划"],
            },
        }
    )
