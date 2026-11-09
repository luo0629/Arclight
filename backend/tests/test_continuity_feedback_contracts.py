"""The model can only request bounded actions over exact immutable identities."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from arclight.domain.continuity_feedback import FeedbackDecision
from arclight.domain.continuity_reviewer import review_operation
from arclight.domain.continuity_reviews import ReviewRerun, ReviewTarget


def decision_value(action="READY_FOR_REVIEW"):
    return {
        "decision_id": str(uuid4()),
        "revision_id": str(uuid4()),
        "review_id": str(uuid4()),
        "review_sha256": "a" * 64,
        "action": action,
        "reason": "The exact new review is complete.",
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"reason": "  "},
        {"reason": "x" * 2001},
        {"action": "SELECT"},
        {"approve": True},
        {"reasoning": "private reasoning"},
        {"review_sha256": "old"},
        {"schema_version": "continuity-feedback/2"},
        {"action": "REVISE"},
        {"revision_id": "invalid"},
        {"decision_id": None},
    ],
)
def test_feedback_rejects_unbounded_or_unsupported_decisions(patch):
    with pytest.raises(ValidationError):
        FeedbackDecision.model_validate(decision_value() | patch)


@pytest.mark.parametrize("action", ["INVESTIGATE", "HUMAN_REVIEW", "READY_FOR_REVIEW"])
def test_only_revision_action_may_include_the_qa05_request(action):
    value = decision_value(action)
    value["revision_request"] = {
        "review_id": value["review_id"],
        "issue_ids": [str(uuid4())],
        "action": "REGENERATE",
        "amendments": [],
    }
    with pytest.raises(ValidationError):
        FeedbackDecision.model_validate(value)


def test_feedback_revision_cannot_substitute_a_previous_review_or_provider_parameters():
    value = decision_value("REVISE")
    value["revision_request"] = {
        "review_id": str(uuid4()),
        "issue_ids": [str(uuid4())],
        "action": "REGENERATE",
        "amendments": [],
    }
    with pytest.raises(ValidationError):
        FeedbackDecision.model_validate(value)
    value["revision_request"]["review_id"] = value["review_id"]
    good = FeedbackDecision.model_validate(value)
    assert good.revision_request.review_id == good.review_id
    value["revision_request"]["provider"] = "another-provider"
    with pytest.raises(ValidationError):
        FeedbackDecision.model_validate(value)


def test_stable_recheck_operation_fits_shared_request_and_preserves_original_operation():
    target = ReviewTarget(
        candidate_id=uuid4(),
        asset_id=uuid4(),
        asset_version_id=uuid4(),
        input_manifest_id=uuid4(),
        media_check_sha256="b" * 64,
        storyboard_id=uuid4(),
        shot_id=uuid4(),
    )
    original = f"continuity-reviewer/1:{target.candidate_id}:{target.shot_id}"
    assert review_operation(target) == original
    rerun = ReviewRerun(previous_review_id=uuid4(), previous_sha256="c" * 64, reason="Read again")
    operation = review_operation(target, rerun)
    assert len(operation) <= 128 and operation == review_operation(target, rerun)
    changed = rerun.model_copy(update={"previous_review_id": uuid4()})
    assert operation != review_operation(target, changed)
    assert operation != review_operation(target.model_copy(update={"shot_id": uuid4()}), rerun)
