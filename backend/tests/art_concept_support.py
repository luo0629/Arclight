"""Synthetic concept-media scenarios over actual HTTP tools and generation services."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import httpx
from art_direction_support import draft_from_feedback
from sqlalchemy import select
from test_agent_model import call_response
from test_agent_runtime import final, tool_feedback
from test_director_workflow import script
from test_generation_candidates import ingest
from test_generation_delivery import deliver
from test_generation_jobs import payload

from arclight.domain.model_gateway import GatewayPolicy
from arclight.persistence.models import GenerationAttempt
from arclight.providers.media_submission import MockMediaSubmissionProvider


def start_concepts(
    client,
    episode,
    settings,
    *,
    rounds=2,
    count=2,
    budget="10",
    limits=None,
    key=None,
    spec_patch=None,
    minimum=None,
):
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost="0", pricing_version="concept-test:1"
    )
    source = script(client, f"/api/v1/episodes/{episode['id']}")
    spec = payload()["spec"] | {
        "context": {},
        "batch_size": count,
        "output_spec": {"width": 8, "height": 12, "format": "png"},
        **(spec_patch or {}),
    }
    body = {
        "script_version_id": source["id"],
        "task": {
            "visual_goal": "比较雨夜配色并据候选修订",
            "minimum_reference_images": count if minimum is None else minimum,
        },
        "concept_plan": {
            "target_type": "STYLE",
            "spec": spec,
            "maximum_rounds": rounds,
            "maximum_candidates": count * rounds,
            "maximum_cost": budget,
        },
    }
    if limits is not None:
        body["agent_limits"] = limits
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/art-runs",
        headers={"Idempotency-Key": key or uuid4().hex},
        json=body,
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"], body


def concept_draft(payload):
    copied = deepcopy(payload)
    for message in copied["messages"]:
        if message["role"] == "tool":
            value = json.loads(message["content"])
            if value["tool_name"] == "observe_art_concepts":
                value["tool_name"] = "investigate_art"
                message["content"] = json.dumps(value)
    draft = draft_from_feedback(copied)
    for reference in draft["proposal"]["references"]:
        reference["usage"] = "COMPARISON"
    return draft


def install_concept_model(model_service, *, rounds=2, on_http=None):
    sent, errors, drafts = [], [], []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if on_http:
            on_http(payload)
        feedback = tool_feedback(payload)
        failed = [row for row in feedback if row["status"] != "SUCCEEDED"]
        if failed:
            errors.extend(failed)
            return httpx.Response(200, json=final("HUMAN_REVIEW"))
        requests = [row for row in feedback if row["tool_name"] == "request_art_concepts"]
        observations = [row for row in feedback if row["tool_name"] == "observe_art_concepts"]
        comparisons = [row for row in feedback if row["tool_name"] == "compare_art_options"]
        if not feedback:
            value = call_response(
                ("visual", "investigate_art", {"query": {"path": "visual"}}),
                ("model", "investigate_art", {"query": {"path": "model"}}),
            )
        elif not requests or len(comparisons) == len(requests) < rounds:
            number = len(requests) + 1
            arguments = {
                "round_no": number,
                "prompt": f"第{number}轮降低背景饱和度，保留银发轮廓",
                "rationale": "探索清晰轮廓"
                if number == 1
                else "据已查看候选和比较回执降低背景饱和度",
            }
            if comparisons:
                arguments["previous_comparison_sha256"] = comparisons[-1]["result"]["comparison"][
                    "comparison_sha256"
                ]
            value = call_response((f"generate-{number}", "request_art_concepts", arguments))
        elif len(observations) < len(requests):
            value = call_response(
                (
                    f"observe-{len(requests)}",
                    "observe_art_concepts",
                    {"job_id": requests[-1]["result"]["job_id"]},
                )
            )
        elif observations[-1]["result"]["outcome_uncertain"]:
            value = final("HUMAN_REVIEW")
        elif len(comparisons) < len(observations):
            draft = concept_draft(payload)
            drafts.append(draft)
            value = call_response(
                (f"compare-{len(observations)}", "compare_art_options", {"draft": draft})
            )
        else:
            value = final(
                result={
                    "draft": deepcopy(drafts[-1]),
                    "comparison_sha256": comparisons[-1]["result"]["comparison"][
                        "comparison_sha256"
                    ],
                }
            )
        return httpx.Response(200, json=value)

    model_service(respond)
    return sent, errors, drafts


def complete_concept_job(client, job_id, *, provider=None):
    assert (
        deliver(client, {"id": str(job_id)}, provider or MockMediaSubmissionProvider())
        == "SUCCEEDED"
    )
    with client.app.state.session_factory() as db:
        attempt_id = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(str(job_id)))
        )
    return ingest(client, attempt_id)
