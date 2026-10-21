from arclight.providers.text import MockTextProvider


def test_manual_versions_immutable_approved_reference_and_canvas(client, episode):
    prefix = f"/api/v1/episodes/{episode['id']}"
    content = MockTextProvider().script("一个温暖的故事", {}).model_dump()
    first = client.post(prefix + "/scripts", json={"expected_version_no": 0, "content": content})
    assert first.status_code == 201, first.text
    original = first.json()
    assert original["run_id"] is None
    content["title"] = "新标题"
    second = client.post(prefix + "/scripts", json={"expected_version_no": 1, "content": content})
    assert second.status_code == 201, second.text
    assert second.json()["version_no"] == 2
    assert client.get(prefix + "/scripts").json()[1]["content_json"]["title"] != "新标题"
    assert (
        client.post(
            prefix + "/scripts", json={"expected_version_no": 1, "content": content}
        ).status_code
        == 409
    )
    from arclight.agents.contracts import ScriptContent

    board = MockTextProvider().storyboard(ScriptContent.model_validate(content), 30000).model_dump()
    body = {"expected_version_no": 0, "script_version_id": second.json()["id"], "content": board}
    assert client.post(prefix + "/storyboards", json=body).status_code == 409
    url = prefix + f"/scripts/{second.json()['id']}/review"
    assert client.post(url, json={"decision": "APPROVE"}).status_code == 200
    assert client.post(url, json={"decision": "REJECT"}).status_code == 409
    saved_board = client.post(prefix + "/storyboards", json=body)
    assert saved_board.status_code == 201, saved_board.text
    assert (
        client.post(
            prefix + f"/storyboards/{saved_board.json()['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    shots = client.get(prefix + "/shots", params={"storyboard_id": saved_board.json()["id"]}).json()
    assert shots and all(shot["status"] == "APPROVED" for shot in shots)
    canvas = client.get(prefix + "/canvas").json()
    assert {row["kind"] for row in canvas["nodes"]} == {"episode", "script", "storyboard", "shot"}
    assert {
        "source": "script:" + second.json()["id"],
        "target": "storyboard:" + saved_board.json()["id"],
        "id": "script-input:" + saved_board.json()["id"],
    } in canvas["edges"]
    body["expected_version_no"] = 1
    body["content"]["shots"][0]["scene_no"] = 999
    assert client.post(prefix + "/storyboards", json=body).status_code == 422


def test_workflow_content_cannot_bypass_version_bound_review(client, episode, settings):
    from uuid import UUID

    from arclight.application.runtime import execute_run

    prefix = f"/api/v1/episodes/{episode['id']}"
    run = client.post(
        prefix + "/script-runs", json={"story": "开场"}, headers={"Idempotency-Key": "one"}
    ).json()
    execute_run(client.app.state.session_factory, settings, UUID(run["run_id"]), 0)
    script = client.get(prefix + "/scripts").json()[0]
    assert (
        client.post(
            prefix + f"/scripts/{script['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 409
    )
    content = MockTextProvider().script("另一个故事", {}).model_dump()
    assert (
        client.post(
            prefix + "/scripts", json={"expected_version_no": 1, "content": content}
        ).status_code
        == 409
    )


def test_manual_content_owner_review_and_cross_project_isolation(client):
    from test_collaboration import create_episode, create_project, register

    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    prefix = f"/api/v1/episodes/{episode['id']}"
    content = MockTextProvider().script("雨天的故事", {}).model_dump()
    version = client.post(
        prefix + "/scripts", json={"expected_version_no": 0, "content": content}
    ).json()
    review = prefix + f"/scripts/{version['id']}/review"
    member = register(client, "editor@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/members",
            json={"email": member["user"]["email"], "role": "editor"},
        ).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.post(review, json={"decision": "APPROVE"}).status_code == 403
    other_project = create_project(client)
    other_episode = create_episode(client, other_project)
    other_review = f"/api/v1/episodes/{other_episode['id']}/scripts/{version['id']}/review"
    assert client.post(other_review, json={"decision": "APPROVE"}).status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert client.post(review, json={"decision": "APPROVE"}).status_code == 200
