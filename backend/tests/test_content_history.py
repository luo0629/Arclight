from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy import func, select
from test_collaboration import create_episode, create_project, register

from arclight.application.runtime import execute_run
from arclight.persistence.models import AuditEvent, Script
from arclight.providers.text import MockTextProvider


def save_script(client, prefix, expected, base=None):
    body = {
        "expected_version_no": expected,
        "content": MockTextProvider().script(f"story {expected}", {}).model_dump(),
    }
    if base:
        body["base_version_id"] = base
    response = client.post(prefix + "/scripts", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def comparison(client, prefix, row, kind="scripts", base=None):
    params = {"base_version_id": base} if base else {}
    return client.get(prefix + f"/{kind}/{row['id']}/comparison", params=params)


def test_comparison_exact_author_creation_source_and_immutable_reads(client):
    owner = register(client)
    episode = create_episode(client, create_project(client))
    prefix = f"/api/v1/episodes/{episode['id']}"
    first = save_script(client, prefix, 0)
    second = save_script(client, prefix, 1, first["id"])
    third = save_script(client, prefix, 2, first["id"])
    with client.app.state.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(AuditEvent))
    result = comparison(client, prefix, third).json()
    assert result["base"]["id"] == second["id"]
    assert result["target"]["base_version"]["id"] == first["id"]
    assert result["target"]["author"] == {
        "actor_id": owner["user"]["id"],
        "display_name": owner["user"]["display_name"],
    }
    assert result["target"]["origin"] == "manual"
    assert result["target"]["created_at"].endswith("Z")
    assert result["target"]["content_json"] == third["content_json"]
    assert comparison(client, prefix, first).json()["base"] is None
    assert comparison(client, prefix, third, base=first["id"]).json()["base"]["id"] == first["id"]
    same = comparison(client, prefix, third, base=third["id"]).json()
    assert same["base"] == same["target"]
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == before
    assert len(client.get(prefix + "/scripts").json()) == 3
    created_at = comparison(client, prefix, first).json()["target"]["created_at"]
    assert (
        client.post(
            prefix + f"/scripts/{first['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    approved = comparison(client, prefix, first).json()["target"]
    assert approved["created_at"] == created_at
    assert approved["status"] == "APPROVED"


def test_comparison_authorization_episode_type_and_base_validation(client):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    prefix = f"/api/v1/episodes/{episode['id']}"
    first = save_script(client, prefix, 0)
    other = create_episode(client, create_project(client))
    other_prefix = f"/api/v1/episodes/{other['id']}"
    foreign = save_script(client, other_prefix, 0)
    assert comparison(client, prefix, foreign).status_code == 404
    assert comparison(client, prefix, first, base=foreign["id"]).status_code == 404
    assert comparison(client, prefix, first, kind="storyboards").status_code == 404
    invalid = client.post(
        prefix + "/scripts",
        json={
            "expected_version_no": 1,
            "base_version_id": foreign["id"],
            "content": first["content_json"],
        },
    )
    assert invalid.status_code == 404
    assert comparison(client, prefix, first, base="not-a-uuid").status_code == 422
    assert comparison(client, prefix, {"id": str(uuid4())}).status_code == 404
    viewer = register(client, "comparison-viewer@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/members",
            json={"email": viewer["user"]["email"], "role": "viewer"},
        ).status_code
        == 201
    )
    assert (
        client.patch(f"/api/v1/projects/{project['id']}", json={"archived": True}).status_code
        == 200
    )
    client.headers["Authorization"] = "Bearer " + viewer["access_token"]
    assert comparison(client, prefix, first).status_code == 200
    register(client, "comparison-outsider@example.com")
    assert comparison(client, prefix, first).status_code == 404


def test_comparison_exact_dependencies_counts_and_bounded_preview(client, episode):
    from arclight.agents.contracts import ScriptContent

    prefix = f"/api/v1/episodes/{episode['id']}"
    first, second = save_script(client, prefix, 0), save_script(client, prefix, 1)
    for script in (first, second):
        assert (
            client.post(
                prefix + f"/scripts/{script['id']}/review", json={"decision": "APPROVE"}
            ).status_code
            == 200
        )
    board_content = (
        MockTextProvider()
        .storyboard(ScriptContent.model_validate(first["content_json"]), 30000)
        .model_dump()
    )
    boards = []
    for index in range(22):
        response = client.post(
            prefix + "/storyboards",
            json={
                "expected_version_no": index,
                "script_version_id": first["id"] if index < 21 else second["id"],
                "content": board_content,
                **({"base_version_id": boards[0]["id"]} if boards else {}),
            },
        )
        assert response.status_code == 201, response.text
        boards.append(response.json())
    result = comparison(client, prefix, second, base=first["id"]).json()
    assert result["base"]["impact"]["storyboard_count"] == 21
    assert len(result["base"]["impact"]["storyboards"]) == 20
    assert result["base"]["impact"]["storyboards"][0]["id"] == boards[20]["id"]
    assert result["base"]["impact"]["shot_count"] == 21 * len(board_content["shots"])
    assert result["target"]["impact"]["storyboard_count"] == 1
    result = comparison(client, prefix, boards[-1], "storyboards").json()
    assert result["target"]["script_version"]["id"] == second["id"]
    assert result["base"]["script_version"]["id"] == first["id"]
    assert result["target"]["base_version"]["id"] == boards[0]["id"]
    assert result["target"]["impact"]["shot_count"] == len(board_content["shots"])
    run = client.post(
        prefix + "/storyboard-runs",
        json={"script_version_id": first["id"]},
        headers={"Idempotency-Key": "impact-pinned"},
    )
    assert run.status_code == 202, run.text
    result = comparison(client, prefix, second, base=first["id"]).json()
    assert result["base"]["impact"]["director_run_count"] == 1
    assert result["target"]["impact"]["director_run_count"] == 0


def test_generated_comparison_attribution_and_idempotent_start(client, settings):
    owner = register(client)
    episode = create_episode(client, create_project(client))
    prefix = f"/api/v1/episodes/{episode['id']}"
    kwargs = {
        "json": {"story": "test generated attribution"},
        "headers": {"Idempotency-Key": "author-test"},
    }
    first = client.post(prefix + "/script-runs", **kwargs)
    assert first.status_code == 202, first.text
    run_id = first.json()["run_id"]
    assert client.post(prefix + "/script-runs", **kwargs).json()["run_id"] == run_id
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    row = client.get(prefix + "/scripts").json()[0]
    target = comparison(client, prefix, row).json()["target"]
    assert target["origin"] == "generated"
    assert target["author"]["actor_id"] == owner["user"]["id"]
    assert target["run_id"] == run_id
    assert target["base_version"] is None
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.resource_id == UUID(run_id))
            )
            == 1
        )


def test_legacy_unknown_author_and_provenance_do_not_infer_previous(client, episode):
    prefix = f"/api/v1/episodes/{episode['id']}"
    first = save_script(client, prefix, 0)
    with client.app.state.session_factory() as db:
        old = Script(
            episode_id=UUID(episode["id"]),
            run_id=None,
            version_no=2,
            content_json=deepcopy(first["content_json"]),
            status="APPROVED",
        )
        db.add(old)
        db.flush()
        row = {"id": str(old.id)}
        db.commit()
    result = comparison(client, prefix, row).json()
    assert result["base"]["id"] == first["id"]
    assert result["target"]["author"] is None
    assert result["target"]["origin"] == "unknown"
    assert result["target"]["base_version"] is None
