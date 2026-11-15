"""Read-only Producer observations under the project's existing snapshot gate."""

import hashlib
import json
from collections import Counter
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.agent_limits import action_usage
from arclight.application.commands import require_project
from arclight.application.costs import read_costs
from arclight.application.dependencies import (
    exact_version,
    input_usable,
    query_dependencies,
)
from arclight.application.generation_budget import policy_for
from arclight.application.model_gateway import utc
from arclight.application.production_state import read_production_state
from arclight.application.project_production import pending_project_reviews
from arclight.application.project_reads import lock_project_snapshot
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.dependencies import validate_dependency_type
from arclight.domain.producer_research import (
    InvestigateProjectParameters,
    InvestigateProjectResult,
    ProducerBudget,
    ProducerDependencies,
    ProducerDependencyEdge,
    ProducerDependencyInputEvidence,
    ProducerDependencyQuery,
    ProducerInformationGap,
    ProducerPendingReview,
    ProducerProductionSummary,
    ProducerReadVersion,
    ProducerReviewDecision,
    ProducerReviewQuery,
    ProducerReviews,
    ProducerRuntimeBudget,
    ProducerVersion,
)
from arclight.persistence.agent_limits import AgentAction, AgentRunLimit
from arclight.persistence.base import utcnow
from arclight.persistence.models import (
    AssetDependency,
    DependencyManifest,
    DependencyOutput,
    Episode,
    Review,
    WorkflowRun,
)


def _runtime_budget(db: Session, run: WorkflowRun) -> ProducerRuntimeBudget | None:
    control = db.get(AgentRunLimit, run.id, populate_existing=True)
    if control is None:
        return None
    root = db.get(AgentRunLimit, control.root_run_id, populate_existing=True)
    if root is None or root.project_id != control.project_id:
        raise DomainError("AGENT_LIMITS_INVALID", "Agent 根运行限制无效", 409)
    try:
        limits = AgentRunLimits.model_validate(root.limits_json)
    except ValidationError:
        raise DomainError("AGENT_LIMITS_INVALID", "Agent 限制记录无效", 409) from None
    usage = action_usage(db, root.run_id)
    actions: list[AgentAction] = usage["actions"]
    return ProducerRuntimeBudget(
        root_run_id=root.run_id,
        limits=limits,
        deadline_at=utc(root.deadline_at),
        calls=len(actions),
        model_calls=sum(row.kind == "MODEL" for row in actions),
        tool_calls=sum(row.kind == "TOOL" for row in actions),
        actual_cost=usage["actual_cost"],
        pending_estimate=usage["pending_estimate"],
        unknown_cost=usage["unknown_cost"],
    )


def _gap(items: list[ProducerInformationGap], code: str, source_id: UUID | None = None) -> None:
    item = ProducerInformationGap(code=code, source_id=source_id)
    if item not in items:
        items.append(item)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _version(
    db: Session, project_id: UUID, version_id: UUID, reads: list[ProducerReadVersion]
) -> ProducerVersion:
    version, asset = exact_version(db, project_id, version_id)
    reads.extend(
        [
            ProducerReadVersion(
                kind="asset", id=asset.id, revision=asset.revision, status=asset.status
            ),
            ProducerReadVersion(
                kind="asset_version",
                id=version.id,
                revision=version.revision,
                version_no=version.version_no,
                status=version.status,
                checksum=version.checksum,
            ),
        ]
    )
    manifest_id = db.scalar(
        select(DependencyManifest.id)
        .join(DependencyOutput, DependencyOutput.dependency_manifest_id == DependencyManifest.id)
        .where(
            DependencyOutput.asset_version_id == version.id,
            DependencyManifest.project_id == project_id,
            DependencyManifest.target_asset_id == asset.id,
        )
    )
    return ProducerVersion(
        id=version.id,
        asset_id=asset.id,
        asset_type=asset.asset_type,
        version_no=version.version_no,
        checksum=version.checksum,
        status=version.status,
        revision=version.revision,
        stale=version.stale,
        current_pointer=asset.current_version_id == version.id,
        usable=not version.stale
        and asset.status != "ARCHIVED"
        and version.status in {"APPROVED", "LOCKED"},
        manifest_id=manifest_id,
    )


def _manifest_gaps(
    db: Session,
    project_id: UUID,
    version: ProducerVersion,
    query: ProducerDependencyQuery,
    expanded: set[UUID],
    reads: list[ProducerReadVersion],
    missing: list[ProducerInformationGap],
    unknown: list[ProducerInformationGap],
) -> ProducerDependencyInputEvidence | None:
    if version.stale:
        _gap(missing, "STALE_VERSION", version.id)
    if version.manifest_id is None:
        _gap(unknown, "DEPENDENCY_MANIFEST_UNAVAILABLE", version.id)
        return None
    manifest = db.get(DependencyManifest, version.manifest_id, populate_existing=True)
    assert manifest is not None
    reads.append(
        ProducerReadVersion(
            kind="dependency_manifest", id=manifest.id, checksum=manifest.request_fingerprint
        )
    )
    registered = list(
        db.scalars(
            select(AssetDependency)
            .where(
                AssetDependency.target_asset_version_id == version.id,
                AssetDependency.dependency_manifest_id == manifest.id,
            )
            .order_by(AssetDependency.id)
            .execution_options(populate_existing=True)
        )
    )
    registered_pairs = {(row.source_asset_version_id, row.dependency_type) for row in registered}
    issues: Counter[str] = Counter()
    inputs = manifest.snapshot_json.get("inputs", [])
    if not isinstance(inputs, list):
        inputs = []
        issues["INVALID_DEPENDENCY_INPUT"] += 1
    expected_pairs: set[tuple[UUID, str]] = set()
    observations = []
    unexpanded = 0
    for item in inputs:
        # Validate every input, including those after a missing or malformed source.
        observation: dict = {"expected_sha256": _digest(item)}
        observations.append(observation)
        try:
            identity = UUID(str(item["source_asset_version_id"]))
            dependency_type = item["dependency_type"]
            if not isinstance(dependency_type, str):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            observation["error"] = "INVALID_DEPENDENCY_INPUT"
            issues["INVALID_DEPENDENCY_INPUT"] += 1
            unexpanded += 1
            continue
        expected_pairs.add((identity, dependency_type))
        unexpanded += identity not in expanded
        observation["source_version_id"] = str(identity)
        try:
            source, asset = exact_version(db, project_id, identity)
        except DomainError as exc:
            if exc.code != "NOT_FOUND":
                raise
            observation["error"] = "MISSING_DEPENDENCY"
            issues["MISSING_DEPENDENCY"] += 1
            continue
        usable = input_usable(db, source, asset, version.asset_type)
        observation.update(
            asset_id=str(asset.id),
            asset_type=asset.asset_type,
            asset_revision=asset.revision,
            asset_status=asset.status,
            current_version_id=str(asset.current_version_id) if asset.current_version_id else None,
            version_no=source.version_no,
            checksum=source.checksum,
            status=source.status,
            revision=source.revision,
            stale=source.stale,
            usable=usable,
        )
        if (
            item.get("asset_id") != str(asset.id)
            or item.get("asset_type") != asset.asset_type
            or item.get("version_no") != source.version_no
            or item.get("checksum") != source.checksum
        ):
            issues["INVALID_DEPENDENCY_INPUT"] += 1
        try:
            validate_dependency_type(asset.asset_type, version.asset_type, dependency_type)
        except DomainError as exc:
            issues[exc.code] += 1
        if not usable:
            issues["STALE_DEPENDENCY" if source.stale else "UNUSABLE_DEPENDENCY"] += 1
    difference = expected_pairs.symmetric_difference(registered_pairs)
    if difference:
        issues["MISSING_DEPENDENCY"] += len(difference)
    for code in sorted(issues):
        # Manifest-wide findings remain bounded; current page edges identify exact sources.
        _gap(missing, code, version.id)
    if unexpanded:
        _gap(unknown, "DEPENDENCY_INPUT_DETAILS_NOT_EXPANDED", version.id)
    evidence = _digest(
        {
            "query": query.model_dump(mode="json"),
            "target_version_id": str(version.id),
            "manifest_id": str(manifest.id),
            "manifest_sha256": manifest.request_fingerprint,
            "snapshot_sha256": _digest(manifest.snapshot_json),
            "inputs": observations,
            "registered_edges": [
                {
                    "id": str(row.id),
                    "source_version_id": str(row.source_asset_version_id),
                    "dependency_type": row.dependency_type,
                }
                for row in registered
            ],
        }
    )
    return ProducerDependencyInputEvidence(
        target_version_id=version.id,
        manifest_id=manifest.id,
        manifest_sha256=manifest.request_fingerprint,
        input_count=len(inputs),
        unexpanded_input_count=unexpanded,
        registered_input_count=len(registered),
        evidence_sha256=evidence,
        issues=dict(sorted(issues.items())),
    )


def _dependencies(
    db: Session,
    project_id: UUID,
    query: ProducerDependencyQuery,
    reads: list[ProducerReadVersion],
    missing: list[ProducerInformationGap],
    unknown: list[ProducerInformationGap],
) -> ProducerDependencies:
    selected = [_version(db, project_id, identity, reads) for identity in query.version_ids]
    rows = query_dependencies(
        db,
        project_id,
        version_ids=query.version_ids,
        direction=query.direction,
        limit=query.limit + 1,
        offset=query.offset,
    )
    expanded = set(query.version_ids)
    for row in rows[: query.limit]:
        expanded.update((row.source_asset_version_id, row.target_asset_version_id))
    input_evidence = []
    for version in selected:
        evidence = _manifest_gaps(db, project_id, version, query, expanded, reads, missing, unknown)
        if evidence is not None:
            input_evidence.append(evidence)
    edges = []
    for row in rows[: query.limit]:
        source, asset = exact_version(db, project_id, row.source_asset_version_id)
        target = _version(db, project_id, row.target_asset_version_id, reads)
        _version(db, project_id, source.id, reads)
        usable = input_usable(db, source, asset, target.asset_type)
        if not usable:
            _gap(missing, "STALE_DEPENDENCY" if source.stale else "UNUSABLE_DEPENDENCY", source.id)
        edges.append(
            ProducerDependencyEdge(
                id=row.id,
                source_version_id=source.id,
                target_version_id=target.id,
                dependency_type=row.dependency_type,
                manifest_id=row.dependency_manifest_id,
                source_usable=usable,
            )
        )
    return ProducerDependencies(
        selected_versions=selected,
        edges=edges,
        input_evidence=input_evidence,
        has_more=len(rows) > query.limit,
        limit=query.limit,
        offset=query.offset,
    )


def _reviews(
    db: Session,
    project_id: UUID,
    episode_id: UUID,
    actor: str,
    query: ProducerReviewQuery,
    reads: list[ProducerReadVersion],
    missing: list[ProducerInformationGap],
) -> ProducerReviews:
    selected = [_version(db, project_id, identity, reads) for identity in query.version_ids]
    for version in selected:
        if version.stale:
            _gap(missing, "STALE_VERSION", version.id)
    page = pending_project_reviews(
        db, project_id, actor, episode_id=episode_id, limit=query.limit, offset=query.offset
    )
    pending = [
        ProducerPendingReview.model_validate(
            item.model_dump(exclude={"project_id", "episode_no", "episode_title", "detail_url"})
        )
        for item in page.items
    ]
    rows = list(
        db.scalars(
            select(Review)
            .join(WorkflowRun, WorkflowRun.id == Review.run_id)
            .join(Episode, Episode.id == WorkflowRun.episode_id)
            .where(Episode.project_id == project_id, Review.version_id.in_(query.version_ids))
            .order_by(Review.created_at.desc(), Review.id)
            .limit(query.limit + 1)
            .offset(query.offset)
            .execution_options(populate_existing=True)
        )
    )
    decisions = [
        ProducerReviewDecision(
            id=row.id,
            run_id=row.run_id,
            resource_type=row.resource_type,
            version_id=row.version_id,
            reviewer_id=row.reviewer_id,
            decision=row.decision,
            created_at=utc(row.created_at),
        )
        for row in rows[: query.limit]
    ]
    reads.extend(
        ProducerReadVersion(kind="review", id=row.id, status=row.decision)
        for row in rows[: query.limit]
    )
    return ProducerReviews(
        pending=pending,
        selected_versions=selected,
        decisions=decisions,
        total=page.total,
        has_more=page.offset + len(page.items) < page.total or len(rows) > query.limit,
        limit=query.limit,
        offset=query.offset,
    )


def investigate_project(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    actor: str,
    parameters: InvestigateProjectParameters,
) -> dict:
    """Observe current evidence; later execution must independently revalidate its gates."""
    current = db.get(Episode, run.episode_id, populate_existing=True)
    if current is None:
        raise DomainError("NOT_FOUND", "剧集不存在", 404)
    project_id = current.project_id
    # Preserve gateway -> run -> project order, including direct service callers.
    project = require_project(db, project_id, actor, write=True)
    lock_project_snapshot(db, project_id)
    db.refresh(current)
    if current.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    query = parameters.query
    requested_episode = getattr(query, "episode_id", None) or current.id
    episode = db.get(Episode, requested_episode, populate_existing=True)
    if episode is None or episode.project_id != project_id:
        raise DomainError("NOT_FOUND", "剧集不存在", 404)
    read_at = utcnow()
    reads = [
        ProducerReadVersion(kind="project", id=project.id, updated_at=utc(project.updated_at)),
        ProducerReadVersion(
            kind="episode", id=episode.id, status=episode.status, updated_at=utc(episode.updated_at)
        ),
    ]
    missing: list[ProducerInformationGap] = []
    unknown: list[ProducerInformationGap] = []
    production = None
    budget = None
    dependencies = None
    reviews = None
    episode_count = None
    if query.path == "overview":
        state = read_production_state(db, episode.id, actor)
        production = ProducerProductionSummary.model_validate(state.model_dump(exclude={"shots"}))
        for condition in state.conditions:
            for issue in condition.issues:
                _gap(missing, issue, episode.id)
        for producer in state.unavailable_producers:
            _gap(unknown, f"PRODUCER_UNAVAILABLE:{producer}", episode.id)
        episode_count = (
            db.scalar(
                select(func.count()).select_from(Episode).where(Episode.project_id == project_id)
            )
            or 0
        )
        for identity in (state.script_version_id, state.storyboard_version_id):
            if identity is not None:
                try:
                    _version(db, project_id, identity, reads)
                except DomainError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                    # Historical text can predate the exact asset registry; disclose the gap.
                    reads.append(ProducerReadVersion(kind="text_version", id=identity))
                    _gap(missing, "UNREGISTERED_VERSION", identity)
    if query.path in {"overview", "budget"}:
        project_costs = read_costs(db, project_id, actor)
        episode_costs = read_costs(db, project_id, actor, episode_id=episode.id)
        policy = policy_for(db, project_id)
        if policy in db:
            db.refresh(policy)
        budget = ProducerBudget(
            project_limit=project.budget_limit,
            policy_revision=policy.revision,
            task_limit=policy.task_limit,
            episode_limit=policy.episode_limit,
            batch_limit=policy.batch_limit,
            warning_percent=policy.warning_percent,
            max_candidates_per_target=policy.max_candidates_per_target,
            project_costs=project_costs.summary,
            episode_costs=episode_costs.summary,
        )
        reads.append(
            ProducerReadVersion(kind="budget_policy", id=project.id, revision=policy.revision)
        )
        if project_costs.summary.unpriced_count:
            _gap(unknown, "UNPRICED_COST", project_id)
        if project_costs.summary.estimated_count:
            _gap(unknown, "UNCONFIRMED_COST", project_id)
    if isinstance(query, ProducerDependencyQuery):
        dependencies = _dependencies(db, project_id, query, reads, missing, unknown)
    if isinstance(query, ProducerReviewQuery):
        reviews = _reviews(db, project_id, episode.id, actor, query, reads, missing)
    runtime = _runtime_budget(db, run)
    if runtime is None:
        _gap(unknown, "AGENT_LIMITS_UNAVAILABLE", run.id)
    elif runtime.unknown_cost:
        _gap(unknown, "AGENT_COST_UNKNOWN", runtime.root_run_id)
    unique_reads = {(row.kind, row.id): row for row in reads}
    result = InvestigateProjectResult(
        query=query,
        project_id=project_id,
        episode_id=episode.id,
        read_at=read_at,
        read_versions=sorted(unique_reads.values(), key=lambda row: (row.kind, str(row.id))),
        evidence_sha256="0" * 64,
        production=production,
        budget=budget,
        runtime_budget=runtime,
        dependencies=dependencies,
        reviews=reviews,
        project_episode_count=episode_count,
        missing=missing,
        unknown=unknown,
    )
    stable = result.model_dump(
        mode="json", exclude={"read_at", "evidence_sha256", "runtime_budget"}
    )
    # Runtime flags describe the separately observed root-family usage, not business evidence.
    stable["unknown"] = [
        item
        for item in stable["unknown"]
        if item["code"] not in {"AGENT_LIMITS_UNAVAILABLE", "AGENT_COST_UNKNOWN"}
    ]
    evidence = _digest(stable)
    if (
        parameters.expected_evidence_sha256 is not None
        and parameters.expected_evidence_sha256 != evidence
    ):
        raise DomainError("PRODUCER_SNAPSHOT_CHANGED", "调查依据已变化，请重新读取", 409)
    return result.model_copy(update={"evidence_sha256": evidence}).model_dump(mode="json")
