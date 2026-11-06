"""ART-01 typed suggestions, exact evidence and compatibility without write authority."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_workflow_contracts import contract_fixture

from arclight.domain.art_proposals import ArtProposal, proposal_schema_registration
from arclight.domain.workflow_contracts import WorkflowRunContract

UNKNOWN_ID = "00000000-0000-0000-0000-000000000001"


def proposal_payload(kind="CHARACTER", project_id=None):
    source, option, design = (str(uuid4()) for _ in range(3))
    concept = {
        "kind": kind,
        "design_id": design,
        "target": {"mode": "NEW"},
        "name": "雨夜角色",
        "metadata": {"description": "保留水墨与纸张纹理"},
    }
    profiles = {
        "CHARACTER": {"fixed_features": ["黑发"], "variable_features": ["表情"]},
        "SCENE": {"layout": "河岸，石桥在画面右侧"},
        "PROP": {"appearance": "折叠纸船"},
    }
    if kind in profiles:
        concept["profile"] = profiles[kind]
    if kind == "VISUAL_BIBLE":
        concept = {
            "kind": kind,
            "design_id": design,
            "version_id": None,
            "version_no": 0,
            "content_sha256": "a" * 64,
            "fields": {"palette": "蓝灰"},
        }
    return {
        "project_id": project_id or str(uuid4()),
        "origin": "MANUAL",
        "visual_goal": "保持雨夜的安静与人物轮廓可读性",
        "constraints": ["保留水墨画风"],
        "sources": [{"kind": "MANUAL", "source_id": source, "statement": "人工视觉任务说明"}],
        "options": [
            {
                "option_id": option,
                "title": "灰蓝雨夜",
                "rationale": "用低饱和背景突出人物",
                "designs": [concept],
                "source_ids": [source],
                "prompts": [
                    {
                        "fragment_id": str(uuid4()),
                        "kind": "POSITIVE",
                        "text": "水墨轮廓，灰蓝雨夜，保留纸张纹理。",
                        "design_ids": [design],
                        "source_ids": [source],
                    }
                ],
            }
        ],
        "preferred_option_id": option,
    }


@pytest.mark.parametrize("kind", ["CHARACTER", "SCENE", "PROP", "STYLE", "VISUAL_BIBLE"])
def test_art_proposal_kinds_round_trip_without_losing_partial_fields(kind):
    proposal = ArtProposal.model_validate(proposal_payload(kind))
    encoded = proposal.model_dump_json()
    restored = ArtProposal.model_validate_json(encoded)
    assert restored == proposal and restored.requires_human_review
    if kind == "VISUAL_BIBLE":
        assert restored.model_dump(mode="json")["options"][0]["designs"][0]["fields"] == {
            "palette": "蓝灰"
        }
    baseline = WorkflowRunContract.model_validate(contract_fixture("ART"))
    assert WorkflowRunContract.model_validate_json(baseline.model_dump_json()) == baseline


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), "art-proposal/2"),
        (("project_id",), "not-a-uuid"),
        (("origin",), "PROVIDER"),
        (("requires_human_review",), False),
        (("requires_human_review",), 1),
        (("requires_human_review",), "true"),
        (("visual_goal",), " "),
        (("visual_goal",), "Bearer private-value"),
        (("visual_goal",), "data:image/png;base64,AAAA"),
        (("constraints",), ["同一约束", "同一约束"]),
        (("preferred_option_id",), UNKNOWN_ID),
        (("status",), "APPROVED"),
        (("unlock",), True),
        (("write_assets",), True),
        (("current_version_id",), UNKNOWN_ID),
        (("sources", 0, "authorization"), "private"),
        (("options", 0, "source_ids"), [UNKNOWN_ID]),
        (("options", 0, "reference_ids"), [UNKNOWN_ID]),
        (("options", 0, "designs", 0, "kind"), "IMAGE"),
        (("options", 0, "designs", 0, "profile"), {"layout": "wrong kind"}),
        (("options", 0, "designs", 0, "target", "asset_id"), UNKNOWN_ID),
        (("options", 0, "designs", 0, "metadata", "locked_fields"), []),
        (("options", 0, "prompts", 0, "design_ids"), [UNKNOWN_ID]),
        (("options", 0, "prompts", 0, "kind"), "EXECUTE"),
        (("options", 0, "prompts", 0, "parameters"), {"provider": "openai"}),
    ],
)
def test_art_contract_rejects_invalid_types_evidence_and_authority(path, value):
    payload = proposal_payload()
    selected = payload
    for key in path[:-1]:
        selected = selected[key]
    selected[path[-1]] = value
    with pytest.raises(ValidationError):
        ArtProposal.model_validate(payload)


@pytest.mark.parametrize("part", ["source", "option", "design", "fragment", "selection"])
def test_duplicate_art_identity_or_target_cannot_ambiguously_apply(part):
    payload = proposal_payload()
    option = payload["options"][0]
    if part == "source":
        payload["sources"] *= 2
    elif part == "option":
        payload["options"] *= 2
    elif part == "design":
        repeated = deepcopy(option["designs"][0])
        repeated["design_id"] = str(uuid4())
        option["designs"].append(repeated)
    elif part == "fragment":
        option["prompts"] *= 2
    else:
        option["source_ids"] *= 2
    with pytest.raises(ValidationError):
        ArtProposal.model_validate(payload)


def test_unresolved_questions_are_explicit_and_do_not_create_an_empty_success():
    payload = proposal_payload()
    payload.update(options=[], preferred_option_id=None)
    with pytest.raises(ValidationError):
        ArtProposal.model_validate(payload)
    payload["questions"] = [{"question_id": str(uuid4()), "question": "请补充角色参考图。"}]
    assert ArtProposal.model_validate(payload).requires_human_review
    payload["origin"] = "AGENT"
    with pytest.raises(ValidationError):
        ArtProposal.model_validate(payload)


def test_multiple_options_share_evidence_but_keep_choices_and_prompt_text_exact():
    payload = proposal_payload()
    alternative = deepcopy(payload["options"][0])
    alternative["option_id"] = str(uuid4())
    alternative["designs"][0]["design_id"] = str(uuid4())
    fragment = alternative["prompts"][0]
    fragment["fragment_id"] = str(uuid4())
    fragment["design_ids"] = [alternative["designs"][0]["design_id"]]
    fragment["text"] = "  保留换行\n和末尾空格  "
    payload["options"].append(alternative)
    payload["preferred_option_id"] = alternative["option_id"]
    parsed = ArtProposal.model_validate(payload)
    assert len(parsed.options) == 2
    assert parsed.options[1].prompts[0].text == fragment["text"]
    assert ArtProposal.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize(
    "field,value", [("revision", True), ("revision", "0"), ("lock_version", -1)]
)
def test_existing_asset_base_uses_strict_directory_and_lock_versions(field, value):
    payload = proposal_payload()
    target = {
        "mode": "EXISTING",
        "asset_id": str(uuid4()),
        "revision": 0,
        "lock_version": 0,
        "current_version_id": None,
        "content_sha256": "a" * 64,
    }
    target[field] = value
    payload["options"][0]["designs"][0]["target"] = target
    with pytest.raises(ValidationError):
        ArtProposal.model_validate(payload)


@pytest.mark.parametrize("fault", ["target", "field", "duplicate", "question", "partial"])
def test_lock_conflicts_and_questions_bind_only_to_proposed_content(fault):
    payload = proposal_payload("VISUAL_BIBLE")
    design = payload["options"][0]["designs"][0]
    conflict = {
        "design_id": design["design_id"],
        "field_path": "fields.palette",
        "reason": "需确认锁定配色",
    }
    payload["lock_conflicts"] = [conflict]
    assert ArtProposal.model_validate(payload).lock_conflicts
    if fault == "target":
        conflict["design_id"] = str(uuid4())
    elif fault == "field":
        conflict["field_path"] = "locked_fields"
    elif fault == "duplicate":
        payload["lock_conflicts"] *= 2
    elif fault == "partial":
        conflict["field_path"] = "fields.lighting"
    else:
        payload["questions"] = [
            {"question_id": str(uuid4()), "question": "谁确认？", "design_id": str(uuid4())}
        ]
    with pytest.raises(ValidationError):
        ArtProposal.model_validate(payload)


def test_art_schema_is_bounded_and_registered_without_inventing_execution():
    registration = proposal_schema_registration()
    assert (registration.family, registration.kind, registration.version) == (
        "ART",
        "SCHEMA",
        "art-proposal/1",
    )
    assert registration.definition["authority"] == "proposal-only/1"
    payload = proposal_payload()
    fragment = payload["options"][0]["prompts"][0]
    payload["options"][0]["prompts"] = [
        fragment | {"fragment_id": str(uuid4()), "text": "墨" * 8000} for _ in range(4)
    ]
    with pytest.raises(ValidationError, match="64 KiB"):
        ArtProposal.model_validate(payload)
