"""Exercise private MCP discovery, calls and permission checks with the SDK."""

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_collaboration import register
from test_director_workflow import script
from test_visual_bible import edit

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import (
    AgentToolContext,
    AgentToolRegistry,
    available_agent_tools,
    grant_agent_tools,
)
from arclight.application.shared_agent_reads import (
    agent_tool_declarations,
    grant_professional_agent_tools,
    with_project_reads,
)
from arclight.domain.agent_state import initial_agent_state, transition_agent_step
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.mcp.client import project_read_session
from arclight.mcp.server import create_project_read_server
from arclight.persistence.models import (
    AgentToolInvocation,
    DependencyManifest,
    Episode,
    Project,
    Script,
    WorkflowRun,
)

READ_SCRIPT = "arclight_read_content"
READ_VISUAL = "arclight_read_visual_references"
PREVIEW_IMPACT = "arclight_preview_change_impact"


def active_run(db, episode, workflow_type, stage):
    db_episode = db.get(Episode, UUID(episode["id"]))
    project = db.get(Project, db_episode.project_id)
    identity = uuid4().hex
    run = WorkflowRun(
        episode_id=db_episode.id,
        workflow_type=workflow_type,
        status="RUNNING",
        trace_id=identity,
        idempotency_key=identity,
        request_fingerprint=identity,
        input_json={},
    )
    db.add(run)
    db.flush()
    scope = scope_for_run(run, db_episode.project_id)
    run.agent_state_json = transition_agent_step(
        initial_agent_state(scope), scope, expected_revision=0, step_id=stage, target="RUNNING"
    ).model_dump(mode="json")
    return run, db_episode, project.owner_id


def test_mcp_has_no_public_http_route(client):
    for method in ("GET", "POST"):
        assert client.request(method, "/api/mcp", headers={"Authorization": ""}).status_code == 404
    register(client)
    for method in ("GET", "POST"):
        assert client.request(method, "/api/mcp").status_code == 404


@pytest.mark.parametrize(
    ("role", "workflow_type", "stage", "expected"),
    [
        (
            "producer",
            "producer",
            "producer",
            {"read_production_status", "read_review_evidence", "preview_change_impact"},
        ),
        (
            "screenwriter",
            "story_outline",
            "outline",
            {"read_content", "query_story_facts"},
        ),
        (
            "screenwriter",
            "script_storyboard",
            "script",
            {"read_content", "query_story_facts", "preview_change_impact"},
        ),
        (
            "screenwriter",
            "script_rewrite",
            "rewrite",
            {"read_content", "query_story_facts", "preview_change_impact"},
        ),
        (
            "director",
            "storyboard",
            "storyboard",
            {
                "read_content",
                "query_story_facts",
                "read_shot_context",
                "read_visual_references",
                "preview_change_impact",
            },
        ),
        (
            "art_director",
            "art_proposal",
            "storyboard",
            {"read_content", "read_shot_context", "read_visual_references"},
        ),
        (
            "continuity_reviewer",
            "continuity_review",
            "storyboard",
            {
                "read_content",
                "query_story_facts",
                "read_shot_context",
                "read_visual_references",
                "read_review_evidence",
            },
        ),
    ],
)
def test_runtime_discovers_only_mcp_tools_granted_to_role_and_stage(
    client, episode, role, workflow_type, stage, expected
):
    with client.app.state.session_factory.begin() as db:
        run, _, actor = active_run(db, episode, workflow_type, stage)
        registry = AgentToolRegistry()
        grant = AgentToolGrantRequest(run_id=run.id, agent_name=role, allowed_tools=[])
        grant_professional_agent_tools(db, grant, actor, registry)
        registry = with_project_reads(registry)
        definitions = available_agent_tools(db, registry, run.id, role, actor, stage)
        declarations = agent_tool_declarations(definitions)
        assert {tool.name for tool in declarations} == {f"arclight_{name}" for name in expected}
        assert all(tool.parameters["additionalProperties"] is False for tool in declarations)

        kept = declarations[0].name
        grant_agent_tools(db, grant.model_copy(update={"allowed_tools": [kept]}), actor, registry)
        definitions = available_agent_tools(db, registry, run.id, role, actor, stage)
        assert [tool.name for tool in agent_tool_declarations(definitions)] == [kept]


def test_private_mcp_checks_agent_scope_and_returns_read_only_evidence(client, episode):
    episode_id = episode["id"]
    project_id = episode["project_id"]
    prefix = f"/api/v1/episodes/{episode_id}"
    approved = script(client, prefix)
    draft = script(client, prefix, version=1, approve=False)
    other_episode = client.post(
        f"/api/v1/projects/{project_id}/episodes", json={"episode_no": 2, "title": "第二集"}
    )
    assert other_episode.status_code == 201, other_episode.text
    other_episode_id = other_episode.json()["id"]
    other_script = script(client, f"/api/v1/episodes/{other_episode_id}")
    bible = edit(client, {"id": project_id}, art_style="水彩")
    assert bible.status_code == 201, bible.text

    async def exercise(context):
        names = (READ_SCRIPT, READ_VISUAL, PREVIEW_IMPACT)
        request = {"episode_id": episode_id, "version_id": approved["id"], "scene_no": 1}
        server = create_project_read_server(names, context=context)
        async with project_read_session(server) as session:
            denied = await session.call_tool("arclight_read_production_status", {})
            assert denied.is_error and denied.content[0].text == "AGENT_NOT_ALLOWED"
            tools = (await session.list_tools()).tools
            assert {tool.name for tool in tools} == set(names)
            assert all(
                tool.annotations
                and tool.annotations.read_only_hint
                and tool.input_schema["additionalProperties"] is False
                and tool.output_schema
                for tool in tools
            )

            script_result = await session.call_tool(READ_SCRIPT, request)
            assert not script_result.is_error
            data = script_result.structured_content["data"]
            assert data["version_id"] == approved["id"]
            assert [scene["scene_no"] for scene in data["content"]["scenes"]] == [1]
            assert len(data["content_sha256"]) == 64

            visual = await session.call_tool(
                READ_VISUAL, {"project_id": project_id, "version_id": bible.json()["id"]}
            )
            assert not visual.is_error
            fields = visual.structured_content["data"]["visual_bible"]["fields"]
            assert fields["art_style"] == "水彩"

            impact = await session.call_tool(
                PREVIEW_IMPACT, {"project_id": project_id, "version_id": approved["id"]}
            )
            assert not impact.is_error
            assert impact.structured_content["data"]["source_asset_version_id"] == approved["id"]

            for query, code in (
                ({**request, "version_id": draft["id"]}, "CONTENT_NOT_APPROVED"),
                ({**request, "actor": "service-owner"}, "INVALID_ARGUMENTS"),
                ({**request, "version_id": other_script["id"]}, "NOT_FOUND"),
                (
                    {
                        "episode_id": other_episode_id,
                        "version_id": other_script["id"],
                    },
                    "NOT_FOUND",
                ),
            ):
                error = await session.call_tool(READ_SCRIPT, query)
                assert error.is_error and error.content[0].text == code

        for denied_context in (
            None,
            replace(context, agent_name="producer"),
            replace(context, stage="script_review"),
        ):
            server = create_project_read_server((READ_SCRIPT,), context=denied_context)
            async with project_read_session(server) as session:
                denied = await session.call_tool(READ_SCRIPT, request)
                assert denied.is_error and denied.content[0].text == "AGENT_NOT_ALLOWED"

    def counts(db):
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (Script, DependencyManifest, AgentToolInvocation)
        )

    with client.app.state.session_factory.begin() as db:
        run, db_episode, actor = active_run(db, episode, "storyboard", "storyboard")
        context = AgentToolContext(
            db, run.id, db_episode.project_id, db_episode.id, actor, "director", stage="storyboard"
        )
        before = counts(db)
        asyncio.run(exercise(context))
        assert counts(db) == before
