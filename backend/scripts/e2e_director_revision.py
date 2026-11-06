"""Deterministic HTTP decisions; all checks and patch merges run in the real Runtime."""

import copy
import json

from arclight.domain.director_revision import CHECK_TOOL


def respond(seed, payload):
    from e2e_screenwriter import _completion

    feedback = [
        json.loads(item["content"]) for item in payload["messages"] if item["role"] == "tool"
    ]
    answered = any("human_decision" in str(item.get("content")) for item in payload["messages"])
    parameters = {"operation_key": seed["operation_key"], "source_sha256": seed["source_sha256"]}
    if not feedback:
        return _tool("check-current", parameters)
    checked = feedback[-1].get("result") or {}
    changed = checked.get("content_sha256") not in {None, seed["source_sha256"]}
    if checked.get("accepted") and (changed or checked["valid"]):
        return _completion(
            {
                "decision": "COMPLETE",
                "human_request": None,
                "result": {
                    "schema_version": "director-revision/1",
                    "source_sha256": seed["source_sha256"],
                    "check_sha256": checked["check_sha256"],
                    "decision": "REVISE" if changed else "ACCEPT",
                    "reason": "依据真实工具反馈修订并复查"
                    if changed
                    else "对白、镜头顺序及全部时长检查通过",
                },
            }
        )
    if seed["remaining_patches"] == 0:
        return _completion(
            {
                "decision": "HUMAN_REVIEW",
                "result": None,
                "human_request": {
                    "reason": "CONSTRAINT_CONFLICT",
                    "question": "问题仍需修订，原额度已耗尽；请停止后重新发起。",
                    "options": ["停止后重新发起"] if not answered else [],
                },
            }
        )
    location = next(issue for issue in seed["checks"]["issues"] if issue["scene_id"])
    scene = next(
        scene for scene in seed["source"]["scenes"] if scene["scene_id"] == location["scene_id"]
    )
    script = next(
        scene for scene in seed["script"]["scenes"] if scene["scene_id"] == location["scene_id"]
    )
    shot = copy.deepcopy(scene["shots"][0])
    shot["dialogue_line_ids"] = [line["line_id"] for line in script["dialogue"]]
    return _tool(
        "repair-dialogue",
        parameters
        | {"patch": {"scene_id": scene["scene_id"], "start": 0, "end": 1, "shots": [shot]}},
    )


def _tool(identity, parameters):
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
                            "function": {"name": CHECK_TOOL, "arguments": json.dumps(parameters)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"},
    }
