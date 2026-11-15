import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_collaboration import create_episode, create_project
from test_content_duration import save_script
from test_dependency_validation import frozen_manifest, registry_version
from test_producer_research import (
    execute_producer,
    grant_producer,
    observed,
    prepare_producer,
    producer_call,
)
from test_production_state import finish_text, read

from arclight.domain.agent_tools import AgentToolCall
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    DependencyManifest,
    DependencyOutput,
    Project,
    ProjectMember,
    Review,
    Script,
    WorkflowRun,
)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": {"path": "invented"}},
        {"query": {"path": "overview", "project_id": str(uuid4())}},
        {"query": {"path": "overview"}, "execute": "private-command"},
        {"query": {"path": "dependencies", "version_ids": []}},
        {"query": {"path": "dependencies", "version_ids": [str(uuid4())] * 2}},
        {"query": {"path": "dependencies", "version_ids": [str(uuid4()) for _ in range(9)]}},
        {"query": {"path": "dependencies", "version_ids": ["invalid-version"]}},
        {
            "query": {
                "path": "dependencies",
                "version_ids": [str(uuid4())],
                "direction": "recursive",
            }
        },
        {"query": {"path": "reviews", "limit": 21}},
        {"query": {"path": "reviews", "limit": "1"}},
        {"query": {"path": "reviews", "limit": True}},
        {"query": {"path": "reviews", "offset": -1}},
        {"query": {"path": "reviews", "version_ids": [str(uuid4())] * 2}},
        {"query": {"path": "budget"}, "expected_evidence_sha256": "not-a-hash"},
        {"query": {"path": "budget"}, "expected_evidence_sha256": 3},
    ],
)
def test_invalid_queries_fail_closed_with_bounded_errors(client, episode, settings, arguments):
    registry, run_id, _ = prepare_producer(client, episode, settings)
    feedback = execute_producer(
        client,
        registry,
        AgentToolCall(
            run_id=run_id,
            tool_name="investigate_project",
            idempotency_key="invalid-query",
            arguments=arguments,
        ),
    )
    assert feedback.status == "FAILED" and feedback.error.code == "INVALID_ARGUMENTS"
    assert feedback.result is None
    assert "private-command" not in feedback.model_dump_json()


def test_project_scope_allows_another_own_episode_and_rejects_foreign_sources(
    client, episode, settings
):
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "另一个合法剧集"},
    )
    assert response.status_code == 201, response.text
    own_episode = response.json()
    foreign_project = create_project(client)
    foreign_episode = create_episode(client, foreign_project)
    with client.app.state.session_factory.begin() as db:
        _, foreign_version = registry_version(db, UUID(foreign_project["id"]))
        foreign_version_id = str(foreign_version.id)
    registry, run_id, _ = prepare_producer(client, episode, settings)
    own = observed(
        client,
        registry,
        producer_call(run_id, query={"path": "overview", "episode_id": own_episode["id"]}),
    )
    assert own["episode_id"] == own_episode["id"] and own["project_episode_count"] == 2
    for number, query in enumerate(
        (
            {"path": "overview", "episode_id": foreign_episode["id"]},
            {"path": "budget", "episode_id": foreign_episode["id"]},
            {"path": "reviews", "episode_id": foreign_episode["id"]},
            {"path": "dependencies", "version_ids": [foreign_version_id]},
            {"path": "reviews", "version_ids": [foreign_version_id]},
            {"path": "dependencies", "version_ids": [str(uuid4())]},
        )
    ):
        feedback = execute_producer(
            client, registry, producer_call(run_id, key=f"foreign-{number}", query=query)
        )
        assert feedback.status == "FAILED" and feedback.error.code == "NOT_FOUND"
        assert feedback.result is None
        assert foreign_project["name"] not in feedback.model_dump_json()


@pytest.mark.parametrize("change", ["revoke", "remove", "viewer", "archive", "delete"])
def test_saved_authorization_and_historical_replay_recheck_current_access(
    client, episode, settings, change
):
    actor_id = None
    if change in {"remove", "viewer"}:
        account = client.post(
            "/api/v1/auth/register",
            json={
                "email": "producer-member@example.com",
                "password": " a very good password ",
                "display_name": "调查成员",
            },
        )
        assert account.status_code == 201, account.text
        actor_id = account.json()["user"]["id"]
        with client.app.state.session_factory.begin() as db:
            member = ProjectMember(
                project_id=UUID(episode["project_id"]),
                user_id=UUID(actor_id),
                role="editor",
            )
            db.add(member)
            db.flush()
            member_id = member.id
    registry, run_id, actor_id = prepare_producer(
        client, episode, settings, grant_actor_id=actor_id
    )
    call = producer_call(run_id)
    assert observed(client, registry, call)["project_id"] == episode["project_id"]
    if change == "revoke":
        grant_producer(client, registry, run_id, actor_id, allowed=False)
    else:
        with client.app.state.session_factory.begin() as db:
            if change == "remove":
                db.delete(db.get(ProjectMember, member_id))
            elif change == "viewer":
                db.get(ProjectMember, member_id).role = "viewer"
            elif change == "archive":
                db.get(Project, UUID(episode["project_id"])).archived = True
            else:
                db.get(Project, UUID(episode["project_id"])).deleted_at = datetime.now(UTC)
    for request in (call, producer_call(run_id, key="new-key")):
        feedback = execute_producer(client, registry, request)
        assert feedback.status == "FAILED" and feedback.error.code == "USER_NOT_AUTHORIZED"
        assert feedback.result is None


def test_new_observation_requires_running_unique_active_step(client, episode, settings):
    registry, run_id, _ = prepare_producer(client, episode, settings)
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        state = deepcopy(run.agent_state_json)
        state["steps"][0]["status"] = "PENDING"
        run.agent_state_json = dict(state)
    feedback = execute_producer(client, registry, producer_call(run_id))
    assert feedback.status == "FAILED" and feedback.error.code == "WORKFLOW_NOT_ACTIVE"


def test_exact_dependencies_report_actual_edges_and_stale_source_in_both_directions(
    client, episode, settings
):
    finish_text(client, episode, settings)
    state = read(client, episode)
    script_id, board_id = state["script_version_id"], state["storyboard_version_id"]
    with client.app.state.session_factory() as db:
        expected_edges = {
            (
                str(row.source_asset_version_id),
                str(row.target_asset_version_id),
                row.dependency_type,
            )
            for row in db.scalars(
                select(AssetDependency).where(
                    AssetDependency.target_asset_version_id == UUID(board_id)
                )
            )
        }
    assert expected_edges and any(source == script_id for source, _, _ in expected_edges)
    registry, run_id, _ = prepare_producer(client, episode, settings)
    incoming = {"path": "dependencies", "version_ids": [board_id]}
    first = observed(client, registry, producer_call(run_id, query=incoming))
    dependencies = first["dependencies"]
    assert {
        (row["source_version_id"], row["target_version_id"], row["dependency_type"])
        for row in dependencies["edges"]
    } == expected_edges
    assert dependencies["selected_versions"][0]["id"] == board_id
    assert dependencies["selected_versions"][0]["usable"] is True
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(script_id)).stale = True
    stale = observed(client, registry, producer_call(run_id, key="stale", query=incoming))
    script_edges = [
        row for row in stale["dependencies"]["edges"] if row["source_version_id"] == script_id
    ]
    assert script_edges and all(not row["source_usable"] for row in script_edges)
    assert stale["evidence_sha256"] != first["evidence_sha256"]
    outgoing = observed(
        client,
        registry,
        producer_call(
            run_id,
            key="outgoing",
            query={"path": "dependencies", "version_ids": [script_id], "direction": "outgoing"},
        ),
    )["dependencies"]
    assert outgoing["selected_versions"][0]["stale"] is True
    assert outgoing["selected_versions"][0]["usable"] is False
    assert any(row["target_version_id"] == board_id for row in outgoing["edges"])


def test_dependency_pagination_is_stable_and_preserves_selected_version_evidence(
    client, episode, settings
):
    # Isolated imported registry rows exercise bounded graph reads, without claiming
    # that the synthetic artifacts passed a production workflow.
    with client.app.state.session_factory.begin() as db:
        _, target = registry_version(db, UUID(episode["project_id"]), kind="STORYBOARD")
        target_id = str(target.id)
        for _ in range(3):
            _, source = registry_version(db, UUID(episode["project_id"]), kind="SCRIPT")
            db.add(
                AssetDependency(
                    source_asset_version_id=source.id,
                    target_asset_version_id=target.id,
                    dependency_type="derived_from",
                )
            )
    registry, run_id, _ = prepare_producer(client, episode, settings)
    pages = [
        observed(
            client,
            registry,
            producer_call(
                run_id,
                key=f"page-{offset}",
                query={
                    "path": "dependencies",
                    "version_ids": [target_id],
                    "limit": 1,
                    "offset": offset,
                },
            ),
        )["dependencies"]
        for offset in range(4)
    ]
    assert [len(page["edges"]) for page in pages] == [1, 1, 1, 0]
    assert [page["has_more"] for page in pages] == [True, True, False, False]
    assert len({page["edges"][0]["id"] for page in pages[:3]}) == 3
    assert all(page["selected_versions"][0]["id"] == target_id for page in pages)


@pytest.mark.parametrize("missing", ["version_registry", "actual_edge"])
def test_historical_incomplete_manifest_reports_missing_dependency_without_rewriting_it(
    client, episode, settings, missing
):
    with client.app.state.session_factory.begin() as db:
        target, output = registry_version(db, UUID(episode["project_id"]), manifest_id=uuid4())
        source = registry_version(db, UUID(episode["project_id"]))
        manifest = frozen_manifest(
            db,
            UUID(episode["project_id"]),
            target,
            output,
            [source],
            patch={"source_asset_version_id": str(uuid4())}
            if missing == "version_registry"
            else None,
        )
        db.add(DependencyOutput(dependency_manifest_id=manifest.id, asset_version_id=output.id))
        output_id, manifest_id = str(output.id), manifest.id
        original_snapshot = deepcopy(manifest.snapshot_json)
    registry, run_id, _ = prepare_producer(client, episode, settings)
    result = observed(
        client,
        registry,
        producer_call(run_id, query={"path": "dependencies", "version_ids": [output_id]}),
    )
    assert {"code": "MISSING_DEPENDENCY", "source_id": output_id} in result["missing"]
    assert result["dependencies"]["edges"] == []
    with client.app.state.session_factory() as db:
        assert db.get(DependencyManifest, manifest_id).snapshot_json == original_snapshot


def test_maximum_manifest_query_is_bounded_and_tracks_unexpanded_source_changes(
    client, episode, settings
):
    project_id = UUID(episode["project_id"])
    selected_ids, all_source_ids = [], []
    with client.app.state.session_factory.begin() as db:
        for _ in range(8):
            target, output = registry_version(db, project_id, manifest_id=uuid4())
            sources = [registry_version(db, project_id) for _ in range(64)]
            manifest = frozen_manifest(db, project_id, target, output, sources)
            db.add(DependencyOutput(dependency_manifest_id=manifest.id, asset_version_id=output.id))
            for _, source in sources:
                db.add(
                    AssetDependency(
                        source_asset_version_id=source.id,
                        target_asset_version_id=output.id,
                        dependency_type="REFERENCE_DEPENDENCY",
                        dependency_manifest_id=manifest.id,
                    )
                )
                all_source_ids.append(source.id)
            selected_ids.append(str(output.id))
    registry, run_id, _ = prepare_producer(client, episode, settings)
    query = {"path": "dependencies", "version_ids": selected_ids, "limit": 20}
    feedback = execute_producer(client, registry, producer_call(run_id, query=query))
    assert feedback.status == "SUCCEEDED", feedback
    assert feedback.trust == "UNTRUSTED"
    assert len(json.dumps(feedback.model_dump(mode="json")).encode()) <= 65536
    first = feedback.result
    dependencies = first["dependencies"]
    assert len(dependencies["selected_versions"]) == 8
    assert len(dependencies["edges"]) == 20 and dependencies["has_more"] is True
    assert len(first["read_versions"]) <= 66
    summaries = dependencies["input_evidence"]
    assert len(summaries) == 8
    assert all(row["input_count"] == row["registered_input_count"] == 64 for row in summaries)
    assert sum(row["unexpanded_input_count"] for row in summaries) == 512 - 20
    assert any(gap["code"] == "DEPENDENCY_INPUT_DETAILS_NOT_EXPANDED" for gap in first["unknown"])
    page_sources = {row["source_version_id"] for row in dependencies["edges"]}
    unexpanded = next(identity for identity in all_source_ids if str(identity) not in page_sources)
    assert all(row["id"] != str(unexpanded) for row in first["read_versions"])
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, unexpanded).stale = True
    changed = observed(
        client, registry, producer_call(run_id, key="unexpanded-changed", query=query)
    )
    assert changed["evidence_sha256"] != first["evidence_sha256"]
    assert changed["dependencies"]["edges"] == dependencies["edges"]
    assert changed["dependencies"]["input_evidence"] != summaries
    assert (
        sum(
            row["issues"].get("STALE_DEPENDENCY", 0)
            for row in changed["dependencies"]["input_evidence"]
        )
        == 1
    )
    assert any(gap["code"] == "STALE_DEPENDENCY" for gap in changed["missing"])


def test_old_review_decisions_do_not_approve_new_exact_version(client, episode, settings):
    finish_text(client, episode, settings)
    original = read(client, episode)
    created = save_script(client, episode, expected=1)
    assert created.status_code == 201, created.text
    new_id = created.json()["id"]
    registry, run_id, _ = prepare_producer(client, episode, settings)
    result = observed(
        client,
        registry,
        producer_call(
            run_id,
            query={"path": "reviews", "version_ids": [original["script_version_id"], new_id]},
        ),
    )["reviews"]
    selected = {row["id"]: row for row in result["selected_versions"]}
    assert selected[original["script_version_id"]]["status"] == "APPROVED"
    assert selected[new_id]["usable"] is False
    assert selected[new_id]["status"] != "APPROVED"
    assert any(row["version_id"] == new_id for row in result["pending"])
    with client.app.state.session_factory() as db:
        expected = {
            str(row.id)
            for row in db.scalars(
                select(Review).where(Review.version_id == UUID(original["script_version_id"]))
            )
        }
    assert expected
    assert {row["id"] for row in result["decisions"]} == expected
    assert all(row["version_id"] != new_id for row in result["decisions"])
    for row in result["pending"] + result["decisions"]:
        assert datetime.fromisoformat(row["created_at"]).tzinfo is not None
    assert "comment" not in str(result["decisions"])


def test_pending_human_review_preserves_exact_snapshot_blocker(client, episode, settings):
    created = save_script(client, episode)
    assert created.status_code == 201, created.text
    version_id = created.json()["id"]
    registry, run_id, _ = prepare_producer(client, episode, settings)
    reviews = client.get(f"/api/v1/projects/{episode['project_id']}/human-reviews").json()
    request = next(row for row in reviews if row["snapshot"]["version_id"] == version_id)
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(version_id)).stale = True
    result = observed(
        client,
        registry,
        producer_call(run_id, query={"path": "reviews", "version_ids": [version_id]}),
    )["reviews"]
    pending = next(row for row in result["pending"] if row["id"] == request["id"])
    assert pending["source"] == "HUMAN_REVIEW"
    assert pending["version_id"] == version_id
    assert pending["status"] == "BLOCKED"
    assert pending["blocking_code"] == "REVIEW_INPUT_CHANGED"
    assert datetime.fromisoformat(pending["created_at"]).tzinfo is not None
    assert not result["decisions"]
    assert result["selected_versions"][0]["stale"] is True
    assert result["selected_versions"][0]["usable"] is False


def test_legacy_missing_exact_registry_is_reported_as_a_blocker(client, episode, settings):
    registry, run_id, _ = prepare_producer(client, episode, settings)
    with client.app.state.session_factory.begin() as db:
        db.add(
            Script(episode_id=UUID(episode["id"]), version_no=1, status="APPROVED", content_json={})
        )
    result = observed(client, registry, producer_call(run_id))
    assert "MISSING_VERSION_REGISTRY" in result["production"]["issues"]
    assert any(row["code"] == "MISSING_VERSION_REGISTRY" for row in result["missing"])
    assert result["production"]["approved_shot_count"] == 0
