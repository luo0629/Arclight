"""Exercise the real MCP endpoint with the SDK and isolated accounts."""

import asyncio

import httpx2
from conftest import serve_asgi
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select
from test_collaboration import create_episode, create_project, register
from test_director_workflow import script
from test_visual_bible import edit

from arclight.main import create_app
from arclight.persistence.models import AgentToolInvocation, DependencyManifest, Script

EXPECTED_TOOLS = {
    "arclight_read_content",
    "arclight_query_story_facts",
    "arclight_read_shot_context",
    "arclight_read_visual_references",
    "arclight_read_production_status",
    "arclight_read_review_evidence",
    "arclight_preview_change_impact",
}


def test_project_reads_over_mcp_check_current_session_and_project(client, settings):
    register(client, "outside@example.com")
    outside = create_project(client)
    outside_episode = create_episode(client, outside)
    outside_script = script(client, f"/api/v1/episodes/{outside_episode['id']}")

    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    endpoint = f"/api/v1/episodes/{episode['id']}"
    approved = script(client, endpoint)
    draft = script(client, endpoint, version=1, approve=False)
    bible = edit(client, project, art_style="水彩")
    assert bible.status_code == 201, bible.text

    def counts():
        with client.app.state.session_factory() as db:
            return tuple(
                db.scalar(select(func.count()).select_from(model))
                for model in (Script, DependencyManifest, AgentToolInvocation)
            )

    before = counts()
    headers = {"Authorization": "Bearer " + owner["access_token"]}

    async def exercise(url):
        init = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "clientInfo": {"name": "Arclight consumer test", "version": "1"},
                "capabilities": {},
            },
        }
        async with httpx2.AsyncClient(trust_env=False, timeout=15) as http:
            assert (await http.post(url, json=init)).status_code == 401
            development = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
            assert (await http.post(url, json=init, headers=development)).status_code == 401
            untrusted = {**headers, "Origin": "https://other.example"}
            assert (await http.post(url, json=init, headers=untrusted)).status_code == 403

        async with (
            asyncio.timeout(60),
            httpx2.AsyncClient(headers=headers, trust_env=False, timeout=15) as http,
            streamable_http_client(url, http_client=http) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            initialized = await session.initialize()
            assert initialized.server_info.name == "arclight"
            listed = (await session.list_tools()).tools
            assert len(listed) == 7 and {tool.name for tool in listed} == EXPECTED_TOOLS
            assert all(
                tool.annotations
                and tool.annotations.read_only_hint
                and tool.input_schema["additionalProperties"] is False
                and tool.output_schema
                for tool in listed
            )
            request = {
                "episode_id": episode["id"],
                "version_id": approved["id"],
                "scene_no": 1,
            }
            version = await session.call_tool("arclight_read_content", request)
            assert not version.is_error
            data = version.structured_content["data"]
            assert data["version_id"] == approved["id"]
            assert len(data["content"]["scenes"]) == 1
            assert data["content"]["scenes"][0]["scene_no"] == 1
            assert len(data["content_sha256"]) == 64

            visual = await session.call_tool(
                "arclight_read_visual_references",
                {"project_id": project["id"], "version_id": bible.json()["id"]},
            )
            assert not visual.is_error
            fields = visual.structured_content["data"]["visual_bible"]["fields"]
            assert fields["art_style"] == "水彩"

            impact = await session.call_tool(
                "arclight_preview_change_impact",
                {"project_id": project["id"], "version_id": approved["id"]},
            )
            assert not impact.is_error
            assert impact.structured_content["data"]["source_asset_version_id"] == approved["id"]

            for query, code in (
                ({**request, "version_id": draft["id"]}, "CONTENT_NOT_APPROVED"),
                ({**request, "actor": "service-owner"}, "INVALID_ARGUMENTS"),
                ({**request, "version_id": outside_script["id"]}, "NOT_FOUND"),
                (
                    {
                        "episode_id": outside_episode["id"],
                        "version_id": outside_script["id"],
                    },
                    "NOT_FOUND",
                ),
            ):
                error = await session.call_tool("arclight_read_content", query)
                assert error.is_error and error.content[0].text == code

            assert client.post("/api/v1/auth/logout").status_code == 204
            revoked = await http.post(url, json=init)
            assert revoked.status_code == 401

    with serve_asgi(create_app(settings)) as address:
        asyncio.run(exercise(address + "/api/mcp"))
    assert counts() == before
