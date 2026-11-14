"""Read contracts used to recover the per-line audio workspace after a refresh."""

from uuid import UUID, uuid4

import pytest
from test_audio_production import snapshot
from test_audio_regeneration import regeneration_case, registered_audio
from test_audio_tts import create_ok, tts_case
from test_audio_tts_boundaries import boundary_state
from test_model_services import account
from test_native_media_integration import configure

from arclight.persistence.models import ProjectMember
from arclight.providers.media_capabilities import resolve_parameters

__all__ = ["tts_case"]


def test_workspace_recovers_unregistered_and_registered_tasks_without_writes(
    client, settings, tts_case
):
    registered, first, _, _ = registered_audio(client, settings, tts_case)
    second, _ = create_ok(client, regeneration_case(tts_case, registered))
    before = boundary_state(client)
    response = client.get(tts_case["url"])
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["job_id"] for row in rows] == [second["job_id"], first["job_id"]]
    assert rows[0]["asset_version_id"] is None
    assert rows[1]["asset_version_id"] == registered["binding"]["asset_version_id"]
    for row in rows:
        assert row == client.get(tts_case["url"] + f"/{row['job_id']}").json()
        assert row["snapshot_id"] == tts_case["snapshot"]["id"]
        assert row["line_id"] == tts_case["lines"][0]["line_id"]
    assert "first-key" not in response.text and "api_key" not in response.text
    assert boundary_state(client) == before


def test_workspace_task_pages_are_stable_after_several_generations(client, settings, tts_case):
    first, first_task, _, _ = registered_audio(client, settings, tts_case)
    second, second_task, _, _ = registered_audio(
        client, settings, regeneration_case(tts_case, first)
    )
    _, third_task, _, _ = registered_audio(client, settings, regeneration_case(tts_case, second))
    expected = [third_task["job_id"], second_task["job_id"], first_task["job_id"]]
    pages = [
        client.get(tts_case["url"], params={"limit": 1, "offset": offset}) for offset in range(4)
    ]
    assert all(page.status_code == 200 for page in pages)
    assert [page.json()[0]["job_id"] for page in pages[:3]] == expected
    assert pages[3].json() == []
    assert [row["job_id"] for row in client.get(tts_case["url"]).json()] == expected


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 100001}]
)
def test_workspace_task_pagination_rejects_invalid_bounds(client, tts_case, params):
    assert client.get(tts_case["url"], params=params).status_code == 422


def test_workspace_tasks_are_scoped_to_exact_snapshot_and_line(client, tts_case):
    create_ok(client, tts_case)
    other = snapshot(client, tts_case["episode"], tts_case["script"])
    assert other.status_code == 201, other.text
    other_url = tts_case["url"].replace(tts_case["snapshot"]["id"], other.json()["id"])
    assert client.get(other_url).json() == []
    missing_line = tts_case["url"].replace(tts_case["lines"][0]["line_id"], "missing-line")
    assert client.get(missing_line).status_code == 404
    missing_snapshot = tts_case["url"].replace(tts_case["snapshot"]["id"], str(uuid4()))
    assert client.get(missing_snapshot).status_code == 404


def test_workspace_tasks_recheck_membership_and_archived_deleted_projects(client, tts_case):
    create_ok(client, tts_case)
    original = client.get(tts_case["url"]).json()
    owner_header = client.headers["Authorization"]
    member = account(client)
    assert client.get(tts_case["url"]).status_code == 404
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(tts_case["pid"]), user_id=UUID(member), role="viewer"
        )
        db.add(membership)
        db.flush()
        member_id = membership.id
    assert client.get(tts_case["url"]).json() == original
    assert client.post(tts_case["url"], json=tts_case["payload"]).status_code == 403
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, member_id))
    assert client.get(tts_case["url"]).status_code == 404
    client.headers["Authorization"] = owner_header
    path = f"/api/v1/projects/{tts_case['pid']}"
    project = client.get(path).json()
    assert client.patch(path, json={"archived": True}).status_code == 200
    assert client.get(tts_case["url"]).json() == original
    assert client.request("DELETE", path, json={"confirm_name": project["name"]}).status_code == 200
    assert client.get(tts_case["url"]).status_code == 404
    del client.headers["Authorization"]
    assert client.get(tts_case["url"]).status_code == 401


def test_workspace_newest_history_is_optional_and_preserves_legacy_default(
    client, settings, tts_case
):
    first, _, _, _ = registered_audio(client, settings, tts_case)
    registered_audio(client, settings, regeneration_case(tts_case, first))
    path = tts_case["url"].removesuffix("/tts") + "/history"
    before = boundary_state(client)
    legacy = client.get(path).json()
    assert [row["version"]["version_no"] for row in legacy] == [1, 2]
    assert client.get(path, params={"newest_first": True}).json() == legacy[::-1]
    assert (
        client.get(path, params={"newest_first": True, "limit": 1, "offset": 1}).json()
        == legacy[:1]
    )
    assert boundary_state(client) == before


def test_workspace_newest_snapshots_preserve_old_order_and_page_contract(client, tts_case):
    second = snapshot(client, tts_case["episode"], tts_case["script"])
    assert second.status_code == 201, second.text
    path = f"/api/v1/episodes/{tts_case['episode']['id']}/audio-snapshots"
    legacy = client.get(path).json()
    assert [row["id"] for row in legacy] == [tts_case["snapshot"]["id"], second.json()["id"]]
    assert client.get(path, params={"newest_first": True}).json() == legacy[::-1]
    assert (
        client.get(path, params={"newest_first": True, "limit": 1, "offset": 1}).json()
        == legacy[:1]
    )


@pytest.mark.parametrize("protocol", ["openai_tts", "qwen_tts", "siliconflow_tts"])
def test_workspace_speech_fields_match_installed_protocol_without_credentials(
    client, settings, tts_case, protocol
):
    configure(client, settings, "speech", protocol, revision=1)
    path = f"/api/v1/projects/{tts_case['pid']}/generation-jobs/services/speech/configuration"
    response = client.get(path)
    assert response.status_code == 200, response.text
    configuration = response.json()
    assert configuration["supported_parameters"] == sorted(
        resolve_parameters(protocol, configuration["model"])
    )
    assert configuration["configured"] is True
    assert "api_key" not in response.text and "first-key" not in response.text


@pytest.mark.parametrize("model", ["tts-1", "tts-1-hd"])
def test_workspace_speech_fields_respect_exact_model_restrictions(
    client, settings, tts_case, model
):
    response = client.put(
        "/api/v1/me/model-services/speech",
        json={
            "contract_version": 2,
            "mode": "custom",
            "supplier": "custom",
            "model": model,
            "protocol": "openai_tts",
            "api_key": "not-returned",
            "base_url": "https://native.example/v1",
            "expected_revision": 1,
        },
    )
    assert response.status_code == 200, response.text
    configuration = client.get(
        f"/api/v1/projects/{tts_case['pid']}/generation-jobs/services/speech/configuration"
    )
    assert configuration.status_code == 200, configuration.text
    assert set(configuration.json()["supported_parameters"]) == {"voice", "speed"}
    assert "not-returned" not in configuration.text
