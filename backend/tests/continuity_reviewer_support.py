"""Scripted transport uses actual reviewer tools, images, contracts and checkpoints."""

import json
from uuid import UUID

import httpx
from test_agent_model import call_response
from test_agent_runtime import final, tool_feedback
from test_continuity_rules import proposals

from arclight.application.continuity_review_store import require_review_record


def saved_review(client, project_id, review_id, *, actor="service-owner"):
    with client.app.state.session_factory() as db:
        return require_review_record(db, UUID(str(project_id)), UUID(str(review_id)), actor).review


def seed_from_payload(payload):
    for message in payload["messages"]:
        if message["role"] == "user" and isinstance(message.get("content"), str):
            value = json.loads(message["content"]).get("data", {})
            if "required_references" in value:
                return value
    raise AssertionError("Missing frozen reviewer seed")


def install_reviewer_model(
    model_service,
    *,
    dynamic=True,
    human=False,
    difference=None,
    uncertainty=None,
    uncertainty_rule="COSTUME",
    mutate_completion=None,
    on_http=None,
    no_images=False,
    steal_manifest=None,
    bypass=False,
):
    sent, errors, completions = [], [], []

    def respond(request):
        try:
            payload = json.loads(request.content)
            sent.append(payload)
            if on_http:
                on_http(payload)
            seed = seed_from_payload(payload)
            feedback = tool_feedback(payload)
            successful = [f for f in feedback if f["status"] == "SUCCEEDED"]
            research = [
                f["result"] for f in successful if f["tool_name"] == "investigate_continuity"
            ]
            checks = [f["result"] for f in successful if f["tool_name"] == "check_continuity_rules"]
            answered = any(
                m["role"] == "user" and "human_decision" in m.get("content", "")
                for m in payload["messages"]
            )
            turn = str(len(sent))
            if not research:
                value = call_response(
                    (
                        "index-" + turn,
                        "investigate_continuity",
                        {"target": seed["target"], "include_images": False},
                    )
                )
            elif human and not answered:
                value = final("HUMAN_REVIEW")
            elif len(research) == 1:
                identities = research[-1]["reference_version_ids"]
                selected = identities[:2] if dynamic else identities
                value = call_response(
                    (
                        "references-" + turn,
                        "investigate_continuity",
                        {
                            "target": seed["target"],
                            "reference_version_ids": selected,
                            "include_images": not no_images,
                        },
                    )
                )
            elif not checks or checks[-1]["evidence_manifest_id"] != (
                steal_manifest or research[-1]["manifest_id"]
            ):
                value = call_response(
                    (
                        "plan-" + turn,
                        "check_continuity_rules",
                        {
                            "target": seed["target"],
                            "evidence_manifest_id": steal_manifest or research[-1]["manifest_id"],
                        },
                    )
                )
            elif dynamic and len(research) == 2:
                # The next reference selection comes from the actual missing-reference feedback.
                assert any(c["gaps"] for c in checks[-1]["checks"])
                value = call_response(
                    (
                        "follow-doubt-" + turn,
                        "investigate_continuity",
                        {
                            "target": seed["target"],
                            "reference_version_ids": research[-1]["reference_version_ids"],
                            "include_images": not no_images,
                        },
                    )
                )
            elif checks[-1]["suggested_conclusion"] is None:
                comparisons = proposals(checks[-1])
                if difference:
                    item = next(c for c in comparisons if c["rule_id"] == difference[0])
                    item.update(
                        status="DIFFERENCE",
                        risk=difference[1],
                        summary="The red case changes state across the adjacent images.",
                    )
                    candidate = next(
                        c for c in item["citations"] if c["source_kind"] == "CANDIDATE"
                    )
                    candidate["region"] = {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5}
                if uncertainty:
                    item = next(c for c in comparisons if c["rule_id"] == uncertainty_rule)
                    item.update(status="UNDETERMINED", uncertainty=uncertainty)
                if bypass:
                    value = final(
                        result={"evidence_manifest_id": checks[-1]["evidence_manifest_id"]}
                    )
                else:
                    value = call_response(
                        (
                            "compare-" + turn,
                            "check_continuity_rules",
                            {
                                "target": seed["target"],
                                "evidence_manifest_id": steal_manifest
                                or research[-1]["manifest_id"],
                                "comparisons": comparisons,
                            },
                        )
                    )
            else:
                completion = {"evidence_manifest_id": checks[-1]["evidence_manifest_id"]}
                if mutate_completion:
                    mutate_completion(completion, research)
                completions.append(completion)
                value = final(result=completion)
            value["usage"]["actual_cost"] = "0"
            return httpx.Response(200, json=value)
        except Exception as error:
            errors.append(repr(error))
            raise

    model_service(respond)
    return sent, errors, completions
