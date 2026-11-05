"""Deterministic phase decisions over exact, server supplied evidence."""

from collections import Counter
from uuid import UUID

from arclight.domain.production_state import (
    ConditionName,
    EpisodeFacts,
    EpisodePhase,
    EpisodeProductionState,
    ExecutionEvidence,
    IssueCode,
    ReviewEvidence,
    ShotFacts,
    ShotPhase,
    ShotProductionState,
    StageCondition,
    VersionEvidence,
)
from arclight.domain.versions import digest


def review_matches(review: ReviewEvidence | None, version: UUID, source: UUID) -> bool:
    return (
        review is not None and review.version_id == version and review.source_version_id == source
    )


def shot_state(facts: ShotFacts, storyboard_id: UUID | None, ready: bool) -> ShotProductionState:
    issues = list(facts.issues)
    phase = ShotPhase.NOT_STARTED
    media = facts.media
    if facts.storyboard_version_id != storyboard_id:
        issues.append("SOURCE_VERSION_MISMATCH")
    elif not ready:
        issues.append("STORYBOARD_NOT_APPROVED")
    elif not issues:
        phase = ShotPhase.READY
        execution = facts.execution
        if execution and execution.target_id != facts.shot_id:
            issues.append("SOURCE_VERSION_MISMATCH")
            phase = ShotPhase.NEEDS_REVIEW
        elif media is not None:
            if media.stale or media.status in {"STALE", "ARCHIVED"}:
                phase, issues = ShotPhase.NEEDS_REVIEW, ["STALE_INPUT"]
            elif media.status == "REJECTED":
                phase, issues = ShotPhase.REJECTED, ["SELECTION_REJECTED"]
            elif facts.storyboard_version_id not in media.source_version_ids:
                phase, issues = ShotPhase.NEEDS_REVIEW, ["SOURCE_VERSION_MISMATCH"]
            elif not review_matches(facts.file_check, media.version_id, storyboard_id):
                phase, issues = ShotPhase.QA_PENDING, ["MEDIA_CHECK_PENDING"]
            elif facts.file_check is not None and facts.file_check.decision != "PASS":
                phase, issues = ShotPhase.REJECTED, ["MEDIA_CHECK_FAILED"]
            elif not review_matches(facts.continuity, media.version_id, storyboard_id):
                phase, issues = ShotPhase.CONTINUITY_REVIEW, ["CONTINUITY_PENDING"]
            elif facts.continuity is not None and facts.continuity.decision == "HUMAN_REVIEW":
                phase, issues = ShotPhase.NEEDS_REVIEW, ["HUMAN_REVIEW_REQUIRED"]
            elif facts.continuity is not None and facts.continuity.decision != "PASS":
                phase, issues = ShotPhase.REJECTED, ["CONTINUITY_REVISION"]
            elif not review_matches(facts.selection, media.version_id, storyboard_id):
                phase, issues = ShotPhase.READY_FOR_REVIEW, ["SELECTION_PENDING"]
            elif facts.selection is not None and facts.selection.decision != "APPROVE":
                phase, issues = ShotPhase.REJECTED, ["SELECTION_REJECTED"]
            elif not media.approved:
                phase, issues = ShotPhase.READY_FOR_REVIEW, ["ASSET_NOT_APPROVED"]
            else:
                phase = ShotPhase.APPROVED
        elif execution:
            if execution.status in {"CREATED", "QUEUED"}:
                phase = ShotPhase.QUEUED
            elif execution.status in {"SUBMITTING", "SUBMITTED", "RUNNING"}:
                phase = ShotPhase.GENERATING
            elif execution.status == "RETRY_WAIT":
                phase = ShotPhase.RETRY_WAIT
            elif execution.status == "FAILED":
                phase, issues = ShotPhase.FAILED, ["EXECUTION_FAILED"]
            elif execution.status == "CANCELLED":
                phase, issues = ShotPhase.CANCELLED, ["EXECUTION_CANCELLED"]
            elif execution.status == "SUCCEEDED":
                phase, issues = ShotPhase.QA_PENDING, ["MISSING_MEDIA"]
            else:
                phase, issues = ShotPhase.NEEDS_REVIEW, ["RESULT_UNCERTAIN"]
    return ShotProductionState(
        shot_id=facts.shot_id,
        storyboard_version_id=facts.storyboard_version_id,
        phase=phase,
        media_version_id=media.version_id if media else None,
        issues=list(dict.fromkeys(issues)),
    )


def version_issues(
    version: VersionEvidence | None, missing: IssueCode, sources: set[UUID] | None = None
) -> list[IssueCode]:
    if version is None:
        return [missing]
    if version.stale or version.status in {"STALE", "ARCHIVED"}:
        return ["STALE_INPUT"]
    if sources is not None and not sources <= set(version.source_version_ids):
        return ["SOURCE_VERSION_MISMATCH"]
    if not version.approved:
        return ["ASSET_NOT_APPROVED"]
    return []


def execution_phase(
    phase: EpisodePhase, execution: ExecutionEvidence | None, target_id: UUID
) -> tuple[EpisodePhase, EpisodePhase | None, list[IssueCode]]:
    if execution and execution.target_id != target_id:
        return phase, phase, ["SOURCE_VERSION_MISMATCH"]
    if execution and execution.status == "FAILED":
        return EpisodePhase.FAILED, phase, ["EXECUTION_FAILED"]
    if execution and execution.status == "CANCELLED":
        return EpisodePhase.CANCELLED, phase, ["EXECUTION_CANCELLED"]
    if execution and execution.status in {
        "CANCEL_REQUESTED",
        "RESULT_UNKNOWN",
        "RECONCILING",
        "MANUAL_REVIEW",
    }:
        return phase, phase, ["RESULT_UNCERTAIN"]
    return phase, None, []


def episode_state(facts: EpisodeFacts) -> EpisodeProductionState:
    # Revalidate even constructed/copied instances; internal callers cannot bypass scope checks.
    facts = EpisodeFacts.model_validate(facts.model_dump(mode="json"))
    script, board = facts.script, facts.storyboard
    script_issues = version_issues(script, "MISSING_SCRIPT")
    board_issues = version_issues(
        board, "MISSING_STORYBOARD", {script.version_id} if script else set()
    )
    board_ready = not script_issues and not board_issues and not facts.input_issues
    shots = [
        shot_state(row, board.version_id if board else None, board_ready) for row in facts.shots
    ]
    shot_issues: list[IssueCode] = []
    if not shots:
        shot_issues.append("MISSING_SHOTS")
    for shot in shots:
        if shot.phase != ShotPhase.APPROVED:
            shot_issues.extend(shot.issues or ["MISSING_MEDIA"])
    shot_issues = list(dict.fromkeys(shot_issues))
    timeline_sources = {shot.media_version_id for shot in shots if shot.media_version_id}
    if board:
        timeline_sources.add(board.version_id)
    timeline_issues = version_issues(facts.timeline, "MISSING_TIMELINE", timeline_sources)
    render_issues = version_issues(
        facts.render, "MISSING_RENDER", {facts.timeline.version_id} if facts.timeline else set()
    )
    delivery_issues: list[IssueCode] = []
    if (
        not facts.render
        or not facts.timeline
        or not review_matches(
            facts.delivery_review, facts.render.version_id, facts.timeline.version_id
        )
        or facts.delivery_review is None
        or facts.delivery_review.decision != "APPROVE"
    ):
        delivery_issues.append("DELIVERY_REVIEW_PENDING")
    acceptance = facts.warning_acceptance
    warnings_confirmed = not facts.warnings or bool(
        facts.render
        and facts.timeline
        and acceptance
        and review_matches(acceptance, facts.render.version_id, facts.timeline.version_id)
        and acceptance.decision == "APPROVE"
        and set(acceptance.warnings) == set(facts.warnings)
    )
    warning_issues: list[IssueCode] = [] if warnings_confirmed else ["WARNINGS_NOT_CONFIRMED"]
    condition_specs: list[tuple[ConditionName, list[IssueCode]]] = [
        ("SCRIPT_APPROVED", script_issues),
        ("ASSETS_USABLE", facts.input_issues),
        ("STORYBOARD_APPROVED", board_issues),
        ("SHOTS_APPROVED", shot_issues),
        ("TIMELINE_READY", timeline_issues),
        ("RENDER_READY", render_issues),
        ("DELIVERY_APPROVED", delivery_issues),
        ("WARNINGS_CONFIRMED", warning_issues),
    ]
    conditions = [
        StageCondition(condition=name, satisfied=not issues, issues=issues)
        for name, issues in condition_specs
    ]
    blocked_phase = None
    issues: list[IssueCode] = []
    if script_issues:
        phase = (
            EpisodePhase.DRAFT
            if script is None and facts.script_execution is None
            else EpisodePhase.SCRIPTING
        )
        if script and script.status == "READY_FOR_REVIEW" and not script.stale:
            phase = EpisodePhase.WAITING_SCRIPT_REVIEW
        phase, blocked_phase, issues = execution_phase(
            phase, facts.script_execution, facts.episode_id
        )
        issues = list(dict.fromkeys([*issues, *script_issues]))
    elif facts.input_issues:
        phase, issues = EpisodePhase.ASSET_PREPARATION, facts.input_issues
    elif board_issues:
        phase = EpisodePhase.STORYBOARDING
        if board and board.status == "READY_FOR_REVIEW" and board_issues == ["ASSET_NOT_APPROVED"]:
            phase = EpisodePhase.WAITING_STORYBOARD_REVIEW
        phase, blocked_phase, issues = execution_phase(
            phase, facts.storyboard_execution, facts.episode_id
        )
        issues = list(dict.fromkeys([*issues, *board_issues]))
    elif shot_issues:
        phase = EpisodePhase.GENERATING_MEDIA
        if shots and all(shot.phase == ShotPhase.CANCELLED for shot in shots):
            phase, blocked_phase = EpisodePhase.CANCELLED, phase
        elif any(shot.phase in {ShotPhase.FAILED, ShotPhase.REJECTED} for shot in shots):
            phase, blocked_phase = EpisodePhase.FAILED, phase
        elif any(
            shot.phase
            in {
                ShotPhase.QA_PENDING,
                ShotPhase.CONTINUITY_REVIEW,
                ShotPhase.READY_FOR_REVIEW,
                ShotPhase.NEEDS_REVIEW,
            }
            for shot in shots
        ):
            phase = EpisodePhase.MEDIA_QA
        issues = shot_issues
    elif timeline_issues:
        phase, issues = EpisodePhase.EDITING, timeline_issues
    elif render_issues:
        phase, issues = EpisodePhase.RENDERING, render_issues
    elif delivery_issues or warning_issues:
        phase, issues = EpisodePhase.DELIVERY_QA, [*delivery_issues, *warning_issues]
    else:
        phase = EpisodePhase.COMPLETED_WITH_WARNINGS if facts.warnings else EpisodePhase.COMPLETED
    executions = [
        row
        for row in (facts.script_execution, facts.storyboard_execution, *facts.media_executions)
        if row
    ]
    executions.extend(shot.execution for shot in facts.shots if shot.execution)
    by_id = {row.execution_id: row for row in executions}
    if any(by_id[row.execution_id] != row for row in executions):
        raise ValueError("Conflicting execution evidence")
    task_counts: Counter[str] = Counter(str(row.status) for row in by_id.values())
    task_counts.update(facts.unbound_media_counts)
    task_issues: list[IssueCode] = []
    if task_counts["FAILED"]:
        task_issues.append("EXECUTION_FAILED")
    if task_counts["CANCELLED"]:
        task_issues.append("EXECUTION_CANCELLED")
    if any(
        task_counts[state]
        for state in ("CANCEL_REQUESTED", "RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW")
    ):
        task_issues.append("RESULT_UNCERTAIN")
    shot_counts = Counter(shot.phase for shot in shots)
    failures = sum(shot_counts[state] for state in (ShotPhase.FAILED, ShotPhase.REJECTED))
    return EpisodeProductionState(
        project_id=facts.project_id,
        episode_id=facts.episode_id,
        archived=facts.archived,
        phase=phase,
        blocked_phase=blocked_phase,
        script_version_id=script.version_id if script else None,
        storyboard_version_id=board.version_id if board else None,
        conditions=conditions,
        issues=issues,
        warnings=facts.warnings,
        warnings_confirmed=warnings_confirmed,
        shot_count=len(shots),
        approved_shot_count=shot_counts[ShotPhase.APPROVED],
        shot_counts=dict(shot_counts),
        task_counts={str(status): count for status, count in task_counts.items()},
        unbound_task_counts={
            str(status): count for status, count in facts.unbound_media_counts.items()
        },
        task_issues=task_issues,
        has_partial_failure=bool(
            (failures and failures < len(shots))
            or (task_counts["FAILED"] and task_counts["FAILED"] < task_counts.total())
        ),
        unavailable_producers=facts.unavailable_producers,
        evidence_sha256=digest(facts.model_dump(mode="json")),
        shots=shots,
    )
