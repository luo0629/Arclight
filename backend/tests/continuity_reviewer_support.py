"""Scripted transport uses actual reviewer tools, images, contracts and checkpoints."""

import json
from copy import deepcopy
from uuid import UUID, uuid4, uuid5

import httpx
from test_agent_model import call_response
from test_agent_runtime import final, tool_feedback
from test_continuity_rules import proposals

from arclight.domain.continuity_reviews import ContinuityReview


def seed_from_payload(payload):
    for message in payload["messages"]:
        if message["role"] == "user" and isinstance(message.get("content"), str):
            value = json.loads(message["content"]).get("data", {})
            if "required_references" in value:
                return value
    raise AssertionError("Missing frozen reviewer seed")


def review_from_feedback(payload, evidence, checked):
    seed = seed_from_payload(payload)
    references = {(r["role"], r["resource_id"]): deepcopy(r) for r in seed["required_references"]}
    for item in evidence["materials"]:
        kind, identity, content = item["source_kind"], item["source_id"], item["content"]
        if kind == "ASSET":
            asset = content["asset"]
            role, checksum = "REFERENCE", asset["checksum"]
            extra = {"asset_id": asset["asset_id"], "asset_type": asset["asset_type"]}
        elif kind == "SHOT":
            role = "TARGET_SHOT" if identity == seed["target"]["shot_id"] else "ADJACENT_SHOT"
            checksum, extra = content["shot_sha256"], {}
        else:
            continue
        references[role, identity] = {
            "reference_id": str(uuid5(UUID(seed["result_id"]), f"{role}:{identity}")),
            "role": role,
            "resource_id": identity,
            "content_sha256": checksum,
            **extra,
        }
    located = [
        {
            "evidence_id": str(uuid4()),
            "reference_id": r["reference_id"],
            "basis": "Fixed candidate compared with the exact observed reference and intent.",
        }
        for r in references.values()
    ]
    issues, uncertainties = [], []
    for check in checked["checks"]:
        comparison = check["comparison"]
        if not comparison:
            continue
        if comparison["uncertainty"] and comparison["uncertainty"] not in uncertainties:
            uncertainties.append(comparison["uncertainty"])
        if comparison["status"] == "DIFFERENCE":
            issues.append(
                {
                    "issue_id": str(uuid4()),
                    "category": check["definition"]["category"],
                    "risk": comparison["risk"],
                    "summary": comparison["summary"],
                    "evidence_ids": [e["evidence_id"] for e in located],
                    "suggested_action": "HUMAN_REVIEW"
                    if comparison["risk"] in {"HIGH", "CRITICAL"}
                    else "REVISE_PROMPT",
                }
            )
    if not checked["complete"] and not uncertainties:
        uncertainties.append(
            {"reason": "MISSING_REFERENCE", "detail": "Required comparison evidence is incomplete."}
        )
    conclusion = checked["suggested_conclusion"]
    ranks = ["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
    return {
        "schema_version": "continuity-review/1",
        **{
            key: deepcopy(seed[key])
            for key in ("project_id", "episode_id", "result_id", "target", "versions", "rerun")
        },
        "references": list(references.values()),
        "conclusion": conclusion,
        "summary": "Evidence-driven review of one fixed candidate.",
        "risk": max((i["risk"] for i in issues), key=ranks.index, default="NONE"),
        "confidence": {
            "kind": "ORDINAL_UNCALIBRATED",
            "level": "LOW" if uncertainties else "HIGH",
            "uncertainty": uncertainties,
        },
        "evidence": located,
        "issues": issues,
        "suggested_action": {
            "PASS": "NONE",
            "REVISION_REQUIRED": "REVISE_PROMPT",
            "HUMAN_REVIEW": "HUMAN_REVIEW",
        }[conclusion],
    }


def install_reviewer_model(
    model_service,
    *,
    dynamic=True,
    human=False,
    difference=None,
    uncertainty=None,
    mutate_final=None,
    on_http=None,
    no_images=False,
    steal_manifest=None,
    bypass=False,
):
    sent, errors, deliveries = [], [], []

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
                if uncertainty:
                    item = next(c for c in comparisons if c["rule_id"] == "COSTUME")
                    item.update(status="UNDETERMINED", uncertainty=uncertainty)
                if bypass:
                    fake_check = deepcopy(checks[-1])
                    fake_check.update(complete=True, suggested_conclusion="PASS")
                    delivery = review_from_feedback(payload, research[-1], fake_check)
                    value = final(result=delivery)
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
                delivery = review_from_feedback(payload, research[-1], checks[-1])
                if mutate_final:
                    mutate_final(delivery)
                ContinuityReview.model_validate_json(json.dumps(delivery), strict=True)
                deliveries.append(delivery)
                value = final(result=delivery)
            value["usage"]["actual_cost"] = "0"
            return httpx.Response(200, json=value)
        except Exception as error:
            errors.append(repr(error))
            raise

    model_service(respond)
    return sent, errors, deliveries
