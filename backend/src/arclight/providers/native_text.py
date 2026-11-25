"""Native text protocol fields that must survive SDK normalization."""

import json
from copy import deepcopy
from uuid import uuid4


def gemini_payload(payload: dict) -> dict:
    contents: list[dict] = []
    system = []
    call_names: dict[str, str] = {}
    call_ids: dict[str, str] = {}
    for message in payload["messages"]:
        role = message["role"]
        content = message.get("content")
        if role == "system":
            system.append({"text": content})
            continue
        if role == "tool":
            parts = [
                {
                    "functionResponse": {
                        "name": call_names[message["tool_call_id"]],
                        "response": {"result": json.loads(content)},
                    }
                }
            ]
            if message["tool_call_id"] in call_ids:
                parts[0]["functionResponse"]["id"] = call_ids[message["tool_call_id"]]
        elif message.get("tool_calls"):
            parts = []
            for call in message["tool_calls"]:
                function = call["function"]
                call_names[call["id"]] = function["name"]
                part = {
                    "functionCall": {
                        "name": function["name"],
                        "args": json.loads(function["arguments"]),
                    }
                }
                if call.get("provider_signature"):
                    part["thoughtSignature"] = call["provider_signature"]
                if call.get("provider_call_id"):
                    call_ids[call["id"]] = call["provider_call_id"]
                    part["functionCall"]["id"] = call["provider_call_id"]
                parts.append(part)
        elif isinstance(content, list):
            parts = []
            for part in content:
                if part["type"] == "text":
                    parts.append({"text": part["text"]})
                else:
                    header, encoded = part["image_url"]["url"].split(",", 1)
                    parts.append(
                        {"inlineData": {"mimeType": header[5:].split(";")[0], "data": encoded}}
                    )
        else:
            parts = [{"text": content}]
        native_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == native_role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": native_role, "parts": parts})
    config: dict = {}
    tools = payload.get("tools", [])
    schema = payload.get("response_format", {}).get("json_schema", {}).get("schema")
    # Function calling and constrained JSON are not composable on every Gemini model.
    # Tool turns use the same schema instruction and strict local final validation.
    if not tools:
        config["responseMimeType"] = "application/json"
        if schema:
            config["responseJsonSchema"] = schema
    elif schema:
        system.append({"text": "Final JSON Schema:\n" + json.dumps(schema, ensure_ascii=False)})
    body: dict = {
        "contents": contents,
        "systemInstruction": {"parts": system},
        "generationConfig": config,
    }
    if tools:
        body["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool["function"]["name"],
                        "description": tool["function"].get("description", ""),
                        "parametersJsonSchema": tool["function"]["parameters"],
                    }
                    for tool in tools
                ]
            }
        ]
    return body


def native_usage(body: dict, protocol: str) -> dict:
    usage = body.get("usageMetadata" if protocol == "gemini" else "usage")
    usage = usage if isinstance(usage, dict) else {}
    return {
        "prompt_tokens": usage.get("promptTokenCount" if protocol == "gemini" else "input_tokens"),
        "completion_tokens": usage.get(
            "candidatesTokenCount" if protocol == "gemini" else "output_tokens"
        ),
    }


def gemini_response(body: dict) -> dict:
    result: dict = {"usage": native_usage(body, "gemini")}
    try:
        return _gemini_choices(body, result)
    except (KeyError, TypeError, ValueError, AttributeError):
        # Let the common parser record completed-but-invalid work with its usage.
        return {"usage": result["usage"]}


def _gemini_choices(body: dict, result: dict) -> dict:
    candidates = body.get("candidates", [])
    if not candidates:
        result["choices"] = [
            {
                "finish_reason": "content_filter" if body.get("promptFeedback") else "invalid",
                "message": {},
            }
        ]
        return result
    if not isinstance(candidates, list) or len(candidates) != 1:
        return result
    candidate = candidates[0]
    parts = candidate.get("content", {}).get("parts", [])
    calls = []
    text = []
    for part in parts:
        if "functionCall" in part:
            function = part["functionCall"]
            calls.append(
                {
                    "id": "call_" + uuid4().hex,
                    "type": "function",
                    "function": {
                        "name": function["name"],
                        "arguments": json.dumps(function.get("args", {}), allow_nan=False),
                    },
                    **(
                        {"provider_signature": part["thoughtSignature"]}
                        if "thoughtSignature" in part
                        else {}
                    ),
                    **({"provider_call_id": function["id"]} if "id" in function else {}),
                }
            )
        elif "text" in part and not part.get("thought"):
            text.append(part["text"])
    reason = candidate.get("finishReason")
    finish = (
        ("tool_calls" if calls else "stop")
        if reason == "STOP"
        else (
            "length"
            if reason == "MAX_TOKENS"
            else "content_filter"
            if reason in {"SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST"}
            else "invalid"
        )
    )
    result["choices"] = [
        {
            "finish_reason": finish,
            "message": {"content": "".join(text), **({"tool_calls": calls} if calls else {})},
        }
    ]
    return result


def qwen_response(body: dict) -> dict:
    result = {"usage": body["usage"]}
    try:
        choices = body.get("output", {}).get("choices")
        if isinstance(choices, list):
            choices = deepcopy(choices)
            for choice in choices:
                content = choice.get("message", {}).get("content")
                if isinstance(content, list):
                    choice["message"]["content"] = "".join(item.get("text", "") for item in content)
        result["choices"] = choices
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return result
