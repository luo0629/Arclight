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


def calls(values, *, turn=0):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": f"{name}_{turn}_{index}",
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
        if value["tool_name"] in {"investigate_art", "observe_art_concepts"}
        and value["status"] == "SUCCEEDED"
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
    references = []
    for material in materials:
        if material["source_kind"] != "ASSET":
            continue
        content = material["content"]
        source_id = identity("source-" + material["source_id"])
        sources.append(
            {
                "kind": "VERSION",
                "source_id": source_id,
                "resource_type": "ASSET_VERSION",
                "resource_id": material["source_id"],
                "content_sha256": content["checksum"],
            }
        )
        references.append(
            {
                "reference_id": identity("reference-" + material["source_id"]),
                "source_id": source_id,
                "asset": {
                    "asset_id": content["asset_id"],
                    "asset_version_id": material["source_id"],
                },
                "asset_type": content["asset"]["asset_type"],
                "usage": "COMPARISON",
                "purpose": "比较实际候选的轮廓与配色",
            }
        )
    reference_ids = [reference["reference_id"] for reference in references]
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
            "reference_ids": reference_ids,
            "prompts": [
                {
                    "fragment_id": identity(f"prompt-{i}"),
                    "kind": "POSITIVE",
                    "text": title + "，朴素服饰，纸张纹理，清晰轮廓。",
                    "design_ids": [identity(f"design-{i}")],
                    "source_ids": source_ids,
                    "reference_ids": reference_ids,
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
            "references": references,
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
                "reference_ids": reference_ids,
            }
            for option in options
        ],
    }


def concept_response(payload, seed, feedback, frozen):
    failed = [item for item in feedback if item["status"] != "SUCCEEDED"]
    observations = [item for item in feedback if item["tool_name"] == "observe_art_concepts"]
    if failed or any(item["result"]["outcome_uncertain"] for item in observations):
        return completion(
            {
                "decision": "HUMAN_REVIEW",
                "result": None,
                "human_request": {
                    "reason": "USER_DECISION",
                    "question": "原概念图任务需要核验，请先处理原结果。",
                    "options": ["停止后核验"],
                },
            }
        )
    requests = [item for item in feedback if item["tool_name"] == "request_art_concepts"]
    comparisons = [item for item in feedback if item["tool_name"] == "compare_art_options"]
    plan = frozen["plan"]
    rounds = min(plan["maximum_rounds"], plan["maximum_candidates"] // plan["spec"]["batch_size"])
    if not requests or len(comparisons) == len(requests) < rounds:
        number = len(requests) + 1
        arguments = {
            "round_no": number,
            "prompt": f"第{number}轮探索纸船与雨夜配色，降低背景饱和度。",
            "rationale": "首轮探索轮廓与配色"
            if number == 1
            else "依据已读取候选和实际比较回执，降低背景饱和度并保留纸张纹理",
        }
        if comparisons:
            arguments["previous_comparison_sha256"] = comparisons[-1]["result"]["comparison"][
                "comparison_sha256"
            ]
        return calls([("request_art_concepts", arguments)], turn=len(feedback))
    if len(observations) < len(requests):
        return calls(
            [("observe_art_concepts", {"job_id": requests[-1]["result"]["job_id"]})],
            turn=len(feedback),
        )
    original = draft(seed, feedback)
    if len(comparisons) < len(observations):
        return calls([("compare_art_options", {"draft": original})], turn=len(feedback))
    return completion(
        {
            "decision": "COMPLETE",
            "human_request": None,
            "result": {
                "draft": original,
                "comparison_sha256": comparisons[-1]["result"]["comparison"]["comparison_sha256"],
            },
        }
    )


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
    elif any(
        m["role"] == "user" and isinstance(m.get("content"), str) and "concept_plan" in m["content"]
        for m in payload["messages"]
    ):
        frozen = next(
            json.loads(m["content"])["data"]["concept_plan"]
            for m in payload["messages"]
            if m["role"] == "user"
            and isinstance(m.get("content"), str)
            and "concept_plan" in m["content"]
        )
        value = concept_response(payload, seed, feedback, frozen)
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


def advance_concepts(factory, settings):
    """Use the existing browser worker thread and original media services."""
    from sqlalchemy import select

    from arclight.application.agent_external_waits import queue_ready_external
    from arclight.application.art_concept_jobs import concept_origin
    from arclight.application.generation_candidates import import_attempt
    from arclight.application.generation_delivery import consume_dispatch
    from arclight.persistence.generation import GenerationAttempt, GenerationDispatch, GenerationJob
    from arclight.persistence.models import WorkflowRun
    from arclight.providers.media_outputs import MockMediaOutputProvider
    from arclight.providers.media_submission import MockMediaSubmissionProvider, SubmissionUncertain

    class UncertainProvider(MockMediaSubmissionProvider):
        def submit(self, request):
            raise SubmissionUncertain("Offline synthetic unknown result")

    with factory() as db:
        pending = []
        for job in db.scalars(
            select(GenerationJob).where(
                GenerationJob.status == "CREATED", GenerationJob.provider == "mock"
            )
        ):
            origin = concept_origin(db, job)
            if origin is None:
                continue
            from uuid import UUID

            run = db.get(WorkflowRun, UUID(origin["run_id"]))
            goal = run.input_json["art_direction"]["task"]["visual_goal"]
            if "取消概念" in goal:
                continue
            dispatch = db.scalar(
                select(GenerationDispatch)
                .where(GenerationDispatch.job_id == job.id)
                .order_by(GenerationDispatch.expected_revision.desc())
            )
            pending.append((job.id, dispatch.id, "未知概念" in goal))
    for job_id, event_id, uncertain in pending:
        status = consume_dispatch(
            factory,
            job_id,
            event_id,
            UncertainProvider() if uncertain else MockMediaSubmissionProvider(),
        )
        if status == "SUCCEEDED":
            with factory() as db:
                attempt_id = db.scalar(
                    select(GenerationAttempt.id)
                    .where(GenerationAttempt.job_id == job_id)
                    .order_by(GenerationAttempt.attempt_no.desc())
                )
            import_attempt(factory, settings, attempt_id, MockMediaOutputProvider())
    queue_ready_external(factory, settings)
