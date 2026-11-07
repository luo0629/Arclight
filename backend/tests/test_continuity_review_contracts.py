from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from arclight.domain.continuity_reviews import ContinuityReview, review_schema_registration

UNKNOWN_ID = "00000000-0000-4000-8000-000000000001"


def review_payload():
    candidate, shot, board = [str(uuid4()) for _ in range(3)]
    references = [
        {
            "reference_id": str(uuid4()),
            "role": role,
            "resource_id": identity,
            "content_sha256": "a" * 64,
        }
        for role, identity in (
            ("CANDIDATE", candidate),
            ("TARGET_SHOT", shot),
            ("STORYBOARD", board),
        )
    ]
    return {
        "result_id": str(uuid4()),
        "project_id": str(uuid4()),
        "episode_id": str(uuid4()),
        "target": {
            "candidate_id": candidate,
            "asset_id": str(uuid4()),
            "asset_version_id": str(uuid4()),
            "input_manifest_id": str(uuid4()),
            "media_check_sha256": "a" * 64,
            "storyboard_id": board,
            "shot_id": shot,
        },
        "references": references,
        "versions": [
            {
                "id": str(uuid4()),
                "family": "REVIEW",
                "kind": kind,
                "name": "fixture." + kind,
                "version": "fixture/1",
                "digest": "a" * 64,
            }
            for kind in ("AGENT", "PROMPT", "SCHEMA", "MODEL")
        ],
        "conclusion": "PASS",
        "summary": "候选画面与本次已提供的精确分镜证据一致。",
        "risk": "NONE",
        "confidence": {"level": "HIGH"},
        "evidence": [
            {
                "evidence_id": str(uuid4()),
                "reference_id": references[0]["reference_id"],
                "region": {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.5},
                "basis": "候选中人物位于分镜要求的画面左侧。",
            }
        ],
        "suggested_action": "NONE",
    }


def with_issue(value, risk="MEDIUM"):
    value.update(conclusion="REVISION_REQUIRED", risk=risk, suggested_action="REVISE_PROMPT")
    value["issues"] = [
        {
            "issue_id": str(uuid4()),
            "category": "COSTUME",
            "risk": risk,
            "summary": "候选外套与参考的锁定颜色不同。",
            "evidence_ids": [value["evidence"][0]["evidence_id"]],
            "suggested_action": "REVISE_PROMPT",
        }
    ]
    return value


@pytest.mark.parametrize("conclusion", ["PASS", "REVISION_REQUIRED", "HUMAN_REVIEW"])
def test_three_review_conclusions_round_trip_with_explicit_advisory_evidence(conclusion):
    value = review_payload()
    if conclusion == "REVISION_REQUIRED":
        with_issue(value)
    if conclusion == "HUMAN_REVIEW":
        with_issue(value, "CRITICAL")
        value.update(conclusion="HUMAN_REVIEW", suggested_action="HUMAN_REVIEW")
        value["confidence"] = {
            "level": "LOW",
            "uncertainty": [
                {"reason": "OCCLUDED", "detail": "人物面部被雨伞遮挡，缺少可核对的面部证据。"}
            ],
        }
    review = ContinuityReview.model_validate(value)
    assert ContinuityReview.model_validate_json(review.model_dump_json()) == review
    assert review.schema_version == "continuity-review/1"
    assert review.confidence.kind == "ORDINAL_UNCALIBRATED"
    assert review.target.shot_id == review.references[1].resource_id
    assert review.model_dump(mode="json")["evidence"][0]["basis"] == value["evidence"][0]["basis"]


@pytest.mark.parametrize(
    "path,replacement",
    [
        (("schema_version",), "continuity-review/2"),
        (("conclusion",), "APPROVED"),
        (("conclusion",), "pass"),
        (("risk",), "HIGH"),
        (("summary",), " "),
        (("summary",), "x" * 501),
        (("confidence", "level"), 0.99),
        (("confidence", "level"), "LOW"),
        (("confidence", "kind"), "CALIBRATED_PROBABILITY"),
        (
            ("confidence", "uncertainty"),
            [{"reason": "MISSING_REFERENCE", "detail": "缺少服装参考。"}],
        ),
        (("suggested_action",), "APPROVE"),
        (("suggested_action",), "REGENERATE"),
        (("target", "candidate_id"), UNKNOWN_ID),
        (("target", "media_check_sha256"), "wrong"),
        (("references", 0, "resource_id"), UNKNOWN_ID),
        (("references", 0, "content_sha256"), "A" * 64),
        (("references", 1, "role"), "REFERENCE"),
        (("references", 0, "asset_id"), UNKNOWN_ID),
        (("versions", 0, "family"), "TEXT"),
        (("versions", 0, "kind"), "MODEL"),
        (("versions", 0, "digest"), "not-a-digest"),
        (("evidence", 0, "reference_id"), UNKNOWN_ID),
        (("evidence", 0, "basis"), "\n"),
        (("evidence", 0, "region", "x"), -0.1),
        (("evidence", 0, "region", "x"), 0.8),
        (("evidence", 0, "region", "width"), 0),
        (("evidence", 0, "region", "height"), True),
        (("evidence", 0, "region", "x"), "0.1"),
        (("evidence", 0, "region", "y"), float("nan")),
        (("evidence", 0, "region", "y"), float("inf")),
    ],
)
def test_invalid_review_shapes_versions_risks_and_regions_are_rejected(path, replacement):
    value = review_payload()
    node = value
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = replacement
    with pytest.raises(ValidationError):
        ContinuityReview.model_validate(value)


@pytest.mark.parametrize(
    "path,field,value",
    [
        ((), "chain_of_thought", "private reasoning"),
        ((), "reasoning", "private reasoning"),
        ((), "current_version_id", UNKNOWN_ID),
        ((), "requires_human_review", False),
        (("evidence", 0), "analysis", "private reasoning"),
        (("confidence",), "probability", 0.99),
        (("target",), "api_key", "synthetic"),
        (("versions", 0), "definition", {"secret": "synthetic"}),
    ],
)
def test_contract_has_no_internal_reasoning_credentials_or_production_write_fields(
    path, field, value
):
    payload = review_payload()
    node = payload
    for part in path:
        node = node[part]
    node[field] = value
    with pytest.raises(ValidationError):
        ContinuityReview.model_validate(payload)


@pytest.mark.parametrize(
    "fault",
    [
        "reference",
        "evidence",
        "issue",
        "unknown_issue_evidence",
        "duplicate_issue_evidence",
        "non_image_region",
        "no_candidate",
    ],
)
def test_review_evidence_is_unambiguous_and_issues_cannot_invent_citations(fault):
    value = with_issue(review_payload())
    if fault == "reference":
        value["references"].append(deepcopy(value["references"][0]))
    elif fault == "evidence":
        value["evidence"].append(deepcopy(value["evidence"][0]))
    elif fault == "issue":
        value["issues"].append(deepcopy(value["issues"][0]))
    elif fault == "unknown_issue_evidence":
        value["issues"][0]["evidence_ids"] = [str(uuid4())]
    elif fault == "duplicate_issue_evidence":
        value["issues"][0]["evidence_ids"] *= 2
    else:
        value["evidence"][0]["reference_id"] = value["references"][1]["reference_id"]
        if fault == "no_candidate":
            value["evidence"][0]["region"] = None
    with pytest.raises(ValidationError):
        ContinuityReview.model_validate(value)


@pytest.mark.parametrize(
    "fault",
    [
        "high_risk",
        "missing_reason",
        "ignored_uncertainty",
        "hidden_issue",
        "empty_revision",
        "auto_human",
        "self_rerun",
        "oversize",
    ],
)
def test_review_cannot_hide_risks_uncertainty_or_unbounded_output(fault):
    value = with_issue(review_payload())
    if fault == "high_risk":
        value["risk"] = value["issues"][0]["risk"] = "HIGH"
    elif fault == "missing_reason":
        value["confidence"]["level"] = "UNKNOWN"
    elif fault == "ignored_uncertainty":
        value["confidence"]["uncertainty"] = [
            {"reason": "CONTRADICTION", "detail": "参考互相矛盾。"}
        ]
    elif fault == "hidden_issue":
        value.update(conclusion="PASS", suggested_action="NONE")
    elif fault == "empty_revision":
        value.update(issues=[], risk="NONE")
    elif fault == "auto_human":
        value.update(conclusion="HUMAN_REVIEW", suggested_action="REGENERATE")
    elif fault == "self_rerun":
        value["rerun"] = {
            "previous_review_id": value["result_id"],
            "previous_sha256": "a" * 64,
            "reason": "重查",
        }
    else:
        value["evidence"] = [
            dict(value["evidence"][0], evidence_id=str(uuid4()), basis="证" * 500)
            for _ in range(100)
        ]
    with pytest.raises(ValidationError):
        ContinuityReview.model_validate(value)


@pytest.mark.parametrize(
    "text",
    [
        "data:image/png;base64,AAAA",
        "Bearer synthetic-credential",
        "https://example.test/?token=synthetic",
    ],
)
def test_brief_business_text_cannot_smuggle_credentials_or_inline_media(text):
    value = review_payload()
    value["summary"] = text
    with pytest.raises(ValidationError):
        ContinuityReview.model_validate(value)


def test_canonical_schema_is_registered_without_model_execution_and_legacy_is_explicit():
    registration = review_schema_registration()
    assert (registration.family, registration.kind, registration.version) == (
        "REVIEW",
        "SCHEMA",
        "continuity-review/1",
    )
    schema = registration.definition["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["conclusion"]["enum"] == [
        "PASS",
        "REVISION_REQUIRED",
        "HUMAN_REVIEW",
    ]
    assert registration.definition["authority"] == "advisory-only/1"
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
    with pytest.raises(ValidationError):
        ContinuityReview.model_validate({"status": "passed", "score": 1})
