"""Offline HTTP decisions for the actual director/art collaboration services."""

import json

from arclight.domain.director_collaboration import OBSERVE_OPINION, REQUEST_OPINION


def respond(seed, payload):
    from e2e_art import calls
    from e2e_director_revision import respond as revise

    if seed["round"] != 0:
        return revise(seed, payload)
    feedback = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
    requested = [item for item in feedback if item["tool_name"] == REQUEST_OPINION]
    observed = [item for item in feedback if item["tool_name"] == OBSERVE_OPINION]
    if not requested:
        clarify = "美术澄清" in json.dumps(seed, ensure_ascii=False)
        return calls(
            [
                (
                    REQUEST_OPINION,
                    {
                        "operation_key": seed["operation_key"],
                        "source_sha256": seed["source_sha256"],
                        "task": {
                            "visual_goal": "澄清导演的冷暖方向"
                            if clarify
                            else "导演的雨夜构图意见",
                            "minimum_reference_images": 0,
                        },
                    },
                )
            ]
        )
    if not observed:
        value = requested[-1]["result"]
        return calls(
            [
                (
                    OBSERVE_OPINION,
                    {
                        key: value[key]
                        for key in ("delegation_id", "operation_key", "source_sha256")
                    },
                )
            ],
            turn=len(feedback),
        )
    messages = [
        message
        for message in payload["messages"]
        if not (
            message["role"] == "tool"
            and json.loads(message["content"])["tool_name"] in {REQUEST_OPINION, OBSERVE_OPINION}
        )
    ]
    value = revise(seed, payload | {"messages": messages})
    message = value["choices"][0]["message"]
    if message.get("content"):
        report = json.loads(message["content"])
        if report.get("result") is not None:
            observation = observed[-1]["result"]
            report["result"]["opinion_decision"] = {
                "delegation_id": observation["delegation_id"],
                "observation_sha256": observation["observation_sha256"],
                "decision": "DECLINE"
                if "拒绝美术" in json.dumps(seed, ensure_ascii=False)
                else "ADOPT",
                "reason": "保留原调度"
                if "拒绝美术" in json.dumps(seed, ensure_ascii=False)
                else "结合真实美术比较保留冷色构图意见",
            }
        message["content"] = json.dumps(report)
    return value
