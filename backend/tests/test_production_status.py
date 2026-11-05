"""Future producer contracts exercise real production decisions without fake database services."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from arclight.domain.production_state import EpisodeFacts
from arclight.domain.production_status import episode_state


def identity(label):
    return str(uuid5(NAMESPACE_URL, "arclight:flow01:" + label))


def evidence():
    scope = {"project_id": identity("project"), "episode_id": identity("episode")}

    def version(label, kind, sources=()):
        return scope | {
            "version_id": identity(label),
            "kind": kind,
            "status": "APPROVED",
            "source_version_ids": [identity(source) for source in sources],
        }

    def review(label, version, source, decision):
        return scope | {
            "review_id": identity(label),
            "version_id": identity(version),
            "source_version_id": identity(source),
            "actor_id": "owner",
            "decision": decision,
        }

    return scope | {
        "script": version("script", "SCRIPT"),
        "storyboard": version("board", "STORYBOARD", ["script"]),
        "shots": [
            scope
            | {
                "shot_id": identity("shot"),
                "storyboard_version_id": identity("board"),
                "media": version("image", "IMAGE", ["board"]),
                "file_check": review("file", "image", "board", "PASS"),
                "continuity": review("continuity", "image", "board", "PASS"),
                "selection": review("selection", "image", "board", "APPROVE"),
            }
        ],
        "timeline": version("timeline", "TIMELINE", ["board", "image"]),
        "render": version("render", "RENDER", ["timeline"]),
        "delivery_review": review("delivery", "render", "timeline", "APPROVE"),
    }


def execution(values, status, *, target="episode", name="execution"):
    return {key: values[key] for key in ("project_id", "episode_id")} | {
        "execution_id": identity(name),
        "target_id": identity(target),
        "status": status,
    }


def change(values, path, value):
    target = values
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    target[parts[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        ("script", None, "DRAFT"),
        ("script.status", "DRAFT", "SCRIPTING"),
        ("script.status", "READY_FOR_REVIEW", "WAITING_SCRIPT_REVIEW"),
        ("input_issues", ["MISSING_ASSET"], "ASSET_PREPARATION"),
        ("storyboard", None, "STORYBOARDING"),
        ("storyboard.status", "READY_FOR_REVIEW", "WAITING_STORYBOARD_REVIEW"),
        ("shots.0.media", None, "GENERATING_MEDIA"),
        ("shots.0.file_check", None, "MEDIA_QA"),
        ("timeline", None, "EDITING"),
        ("render", None, "RENDERING"),
        ("delivery_review", None, "DELIVERY_QA"),
    ],
)
def test_later_artifacts_cannot_skip_a_required_stage(path, value, expected):
    values = evidence()
    change(values, path, value)
    state = episode_state(EpisodeFacts.model_validate(values))
    assert state.phase == expected
    assert state.phase not in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}


def test_complete_requires_every_exact_gate_and_is_repeatable():
    facts = EpisodeFacts.model_validate(evidence())
    first = episode_state(facts)
    assert first.phase == "COMPLETED"
    assert all(condition.satisfied for condition in first.conditions)
    assert first.approved_shot_count == first.shot_count == 1
    assert first.issues == []
    assert episode_state(EpisodeFacts.model_validate(facts.model_dump(mode="json"))) == first


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("CREATED", "QUEUED"),
        ("QUEUED", "QUEUED"),
        ("SUBMITTING", "GENERATING"),
        ("SUBMITTED", "GENERATING"),
        ("RUNNING", "GENERATING"),
        ("RETRY_WAIT", "RETRY_WAIT"),
        ("FAILED", "FAILED"),
        ("CANCELLED", "CANCELLED"),
        ("SUCCEEDED", "QA_PENDING"),
        ("CANCEL_REQUESTED", "NEEDS_REVIEW"),
        ("RESULT_UNKNOWN", "NEEDS_REVIEW"),
        ("RECONCILING", "NEEDS_REVIEW"),
        ("MANUAL_REVIEW", "NEEDS_REVIEW"),
    ],
)
def test_execution_success_and_cancellation_intent_do_not_approve_media(status, expected):
    values = evidence()
    shot = values["shots"][0]
    shot.update(media=None, file_check=None, continuity=None, selection=None)
    shot["execution"] = execution(values, status, target="shot")
    state = episode_state(EpisodeFacts.model_validate(values))
    assert state.shots[0].phase == expected
    assert state.approved_shot_count == 0
    if status == "SUCCEEDED":
        assert state.shots[0].issues == ["MISSING_MEDIA"]
    if status in {"CANCEL_REQUESTED", "RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"}:
        assert state.phase != "CANCELLED"
        assert state.shots[0].issues == ["RESULT_UNCERTAIN"]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("file_check", "QA_PENDING"),
        ("continuity", "CONTINUITY_REVIEW"),
        ("selection", "READY_FOR_REVIEW"),
    ],
)
@pytest.mark.parametrize("wrong_field", ["version_id", "source_version_id"])
def test_reviews_must_match_current_candidate_and_current_storyboard(field, expected, wrong_field):
    values = evidence()
    values["shots"][0][field][wrong_field] = identity("obsolete")
    result = episode_state(EpisodeFacts.model_validate(values))
    assert result.shots[0].phase == expected
    assert result.approved_shot_count == 0


@pytest.mark.parametrize(
    "path",
    [
        "script",
        "storyboard",
        "shots.0.media",
        "timeline",
        "render",
    ],
)
def test_stale_required_inputs_block_completion_even_with_old_approvals(path):
    values = evidence()
    change(values, path + ".stale", True)
    state = episode_state(EpisodeFacts.model_validate(values))
    assert state.phase not in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}
    assert "STALE_INPUT" in state.issues


@pytest.mark.parametrize(
    ("path", "sources"),
    [
        ("storyboard", []),
        ("shots.0.media", []),
        ("timeline", ["board"]),
        ("render", ["script"]),
    ],
)
def test_old_completed_outputs_cannot_satisfy_new_input_versions(path, sources):
    values = evidence()
    change(values, path + ".source_version_ids", [identity(source) for source in sources])
    state = episode_state(EpisodeFacts.model_validate(values))
    assert state.phase not in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}
    assert "SOURCE_VERSION_MISMATCH" in state.issues


def test_partial_failure_and_mixed_cancellation_preserve_per_shot_progress():
    values = evidence()
    second = deepcopy(values["shots"][0])
    second.update(
        shot_id=identity("second"), media=None, file_check=None, continuity=None, selection=None
    )
    second["execution"] = execution(values, "FAILED", target="second")
    values["shots"].append(second)
    failed = episode_state(EpisodeFacts.model_validate(values))
    assert failed.phase == "FAILED" and failed.blocked_phase == "GENERATING_MEDIA"
    assert failed.has_partial_failure
    assert failed.shot_counts == {"APPROVED": 1, "FAILED": 1}
    assert failed.approved_shot_count == 1
    second["execution"]["status"] = "CANCELLED"
    mixed = episode_state(EpisodeFacts.model_validate(values))
    assert mixed.phase != "COMPLETED" and mixed.phase != "CANCELLED"
    values["shots"] = [second]
    assert episode_state(EpisodeFacts.model_validate(values)).phase == "CANCELLED"


def test_warning_completion_requires_confirmation_for_the_exact_render_and_warning_set():
    values = evidence()
    values["warnings"] = ["AUDIO_CLIPPED"]
    assert episode_state(EpisodeFacts.model_validate(values)).phase == "DELIVERY_QA"
    acceptance = deepcopy(values["delivery_review"]) | {
        "review_id": identity("warning-acceptance"),
        "warnings": ["AUDIO_CLIPPED"],
    }
    values["warning_acceptance"] = acceptance
    assert episode_state(EpisodeFacts.model_validate(values)).phase == "COMPLETED_WITH_WARNINGS"
    for field, value in [
        ("version_id", identity("obsolete")),
        ("source_version_id", identity("obsolete")),
        ("warnings", ["OTHER_WARNING"]),
        ("decision", "REJECT"),
    ]:
        changed = deepcopy(values)
        changed["warning_acceptance"][field] = value
        state = episode_state(EpisodeFacts.model_validate(changed))
        assert state.phase == "DELIVERY_QA" and not state.warnings_confirmed


@pytest.mark.parametrize("path", ["script", "render", "shots.0.continuity"])
@pytest.mark.parametrize("field", ["project_id", "episode_id"])
def test_cross_scope_evidence_is_rejected(path, field):
    values = evidence()
    change(values, path + "." + field, identity("foreign"))
    with pytest.raises(ValidationError, match="different scope"):
        EpisodeFacts.model_validate(values)


def test_preview_unknown_fields_duplicate_identities_and_conflicting_receipts_are_rejected():
    values = evidence()
    values["render"]["kind"] = "RENDER_PREVIEW"
    with pytest.raises(ValidationError, match="exact output kind"):
        EpisodeFacts.model_validate(values)
    values = evidence() | {"phase": "COMPLETED"}
    with pytest.raises(ValidationError):
        EpisodeFacts.model_validate(values)
    values = evidence()
    values["shots"].append(deepcopy(values["shots"][0]))
    with pytest.raises(ValidationError, match="Duplicate production identity"):
        EpisodeFacts.model_validate(values)
    values = evidence()
    receipt = execution(values, "SUCCEEDED", target="shot")
    values["shots"][0]["execution"] = receipt
    values["media_executions"] = [receipt]
    assert episode_state(EpisodeFacts.model_validate(values)).task_counts == {"SUCCEEDED": 1}
    values["media_executions"][0] = receipt | {"status": "FAILED"}
    with pytest.raises(ValueError, match="Conflicting execution"):
        episode_state(EpisodeFacts.model_validate(values))


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_text_execution_terminal_outcome_is_reported_without_inventing_an_artifact(status):
    values = evidence()
    values["script"] = None
    values["script_execution"] = execution(values, status)
    state = episode_state(EpisodeFacts.model_validate(values))
    assert state.phase == status and state.blocked_phase == "SCRIPTING"
    assert "MISSING_SCRIPT" in state.issues


@pytest.mark.parametrize(
    ("path", "value", "phase", "issue"),
    [
        ("file_check.decision", "REJECT", "REJECTED", "MEDIA_CHECK_FAILED"),
        ("continuity.decision", "HUMAN_REVIEW", "NEEDS_REVIEW", "HUMAN_REVIEW_REQUIRED"),
        ("continuity.decision", "REVISION_REQUIRED", "REJECTED", "CONTINUITY_REVISION"),
        ("selection.decision", "REJECT", "REJECTED", "SELECTION_REJECTED"),
        ("media.status", "REJECTED", "REJECTED", "SELECTION_REJECTED"),
    ],
)
def test_failed_or_uncertain_quality_gates_cannot_be_laundered_by_later_approvals(
    path, value, phase, issue
):
    values = evidence()
    change(values, "shots.0." + path, value)
    state = episode_state(EpisodeFacts.model_validate(values))
    assert state.shots[0].phase == phase
    assert state.shots[0].issues == [issue]
    assert state.approved_shot_count == 0 and state.phase != "COMPLETED"


@pytest.mark.parametrize("path", ["script", "render", "shots.0.continuity"])
def test_non_shared_evidence_requires_an_exact_episode(path):
    values = evidence()
    change(values, path + ".episode_id", None)
    with pytest.raises(ValidationError):
        EpisodeFacts.model_validate(values)


@pytest.mark.parametrize(
    "status", ["CANCEL_REQUESTED", "RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"]
)
def test_unbound_uncertain_history_is_visible_without_cancelling_required_production(status):
    values = evidence()
    values["shots"][0]["media"] = None
    values["unbound_media_counts"] = {status: 2}
    state = episode_state(EpisodeFacts.model_validate(values))
    assert state.phase == "GENERATING_MEDIA"
    assert state.task_issues == ["RESULT_UNCERTAIN"]
    assert state.unbound_task_counts == {status: 2}
    assert state.task_counts == {status: 2}


@pytest.mark.parametrize("count", [0, -1, True, "1"])
def test_aggregated_database_counts_are_strict_positive_integers(count):
    with pytest.raises(ValidationError):
        EpisodeFacts.model_validate(evidence() | {"unbound_media_counts": {"FAILED": count}})
