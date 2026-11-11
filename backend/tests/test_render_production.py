"""Machine validation feeds the existing production gates without approving assets."""

from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_production_status import change, evidence, identity

from arclight.domain.production_state import EpisodeFacts
from arclight.domain.production_status import episode_state


def validated_facts():
    facts = evidence()
    facts["timeline"]["status"] = "DRAFT"
    facts["render"]["status"] = "DRAFT"
    facts["render_validation"] = {
        **facts["delivery_review"],
        "review_id": identity("machine-report"),
        "actor_id": "arclight-delivery-v1",
        "decision": "PASS",
    }
    return facts


def test_exact_machine_validation_accepts_sealed_drafts_without_mutating_them():
    facts = validated_facts()
    before = deepcopy(facts)
    result = episode_state(EpisodeFacts.model_validate(facts))
    assert result.phase == "COMPLETED"
    assert facts == before
    assert facts["timeline"]["status"] == facts["render"]["status"] == "DRAFT"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("render_validation.version_id", identity("other-render")),
        ("render_validation.source_version_id", identity("other-timeline")),
        ("render_validation.decision", "APPROVE"),
        ("render.stale", True),
        ("timeline.stale", True),
        ("render.status", "REJECTED"),
        ("timeline.status", "REJECTED"),
        ("timeline.source_version_ids", []),
        ("render.source_version_ids", []),
    ],
)
def test_machine_validation_cannot_launder_wrong_or_invalid_production_evidence(path, value):
    facts = validated_facts()
    change(facts, path, value)
    assert episode_state(EpisodeFacts.model_validate(facts)).phase not in {
        "COMPLETED",
        "COMPLETED_WITH_WARNINGS",
    }


def test_machine_validation_is_not_a_human_warning_acceptance():
    facts = validated_facts()
    facts["warnings"] = ["EXPLICIT_SILENCE"]
    facts["delivery_review"] = None
    state = episode_state(EpisodeFacts.model_validate(facts))
    assert state.phase == "DELIVERY_QA"
    assert "WARNINGS_NOT_CONFIRMED" in state.issues
    assert not state.warnings_confirmed


def test_preview_cannot_be_used_as_a_formal_render_even_with_machine_validation():
    facts = validated_facts()
    facts["render"]["kind"] = "RENDER_PREVIEW"
    with pytest.raises(ValidationError, match="exact output kind"):
        EpisodeFacts.model_validate(facts)


@pytest.mark.parametrize("field", ["project_id", "episode_id"])
def test_machine_validation_rejects_cross_scope(field):
    facts = validated_facts()
    facts["render_validation"][field] = identity("other-scope")
    with pytest.raises(ValidationError, match="different scope"):
        EpisodeFacts.model_validate(facts)
