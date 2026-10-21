from uuid import UUID

from arclight.persistence.models import User

PREFIX = "/api/v1"
PASSWORD = " a very good password "


def register(client, email="owner@example.com"):
    response = client.post(
        PREFIX + "/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": "小画家",
        },
    )
    assert response.status_code == 201, response.text
    result = response.json()
    client.headers["Authorization"] = "Bearer " + result["access_token"]
    return result


def create_project(client):
    response = client.post(PREFIX + "/projects", json={"name": "贴纸创作本"})
    assert response.status_code == 201, response.text
    return response.json()


def create_episode(client, project):
    response = client.post(
        f"{PREFIX}/projects/{project['id']}/episodes",
        json={
            "episode_no": 1,
            "title": "初遇",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_auth_register_login_refresh_logout(client):
    owner = register(client, " Owner@EXAMPLE.com ")
    assert owner["user"]["email"] == "owner@example.com"
    assert "refresh_token" not in owner
    assert client.get(PREFIX + "/auth/me").json()["id"] == owner["user"]["id"]
    original = client.cookies.get("arclight_refresh")
    refreshed = client.post(PREFIX + "/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert client.cookies.get("arclight_refresh") != original
    assert "HttpOnly" in refreshed.headers["set-cookie"]
    assert "SameSite=strict" in refreshed.headers["set-cookie"]
    assert refreshed.headers["cache-control"] == "no-store"
    fresh = client.cookies.get("arclight_refresh")
    replay = client.post(
        PREFIX + "/auth/refresh", headers={"Cookie": f"arclight_refresh={original}"}
    )
    assert replay.status_code == 401
    assert (
        client.post(
            PREFIX + "/auth/refresh", headers={"Cookie": f"arclight_refresh={fresh}"}
        ).status_code
        == 200
    )
    assert client.post(PREFIX + "/auth/logout").status_code == 204
    assert client.get(PREFIX + "/auth/me").status_code == 401
    assert client.post(PREFIX + "/auth/refresh").status_code == 401
    # 密码的前后空格是密码的一部分，不能被输入基类悄悄删除。
    assert (
        client.post(
            PREFIX + "/auth/login",
            json={
                "email": "owner@example.com",
                "password": PASSWORD.strip(),
            },
        ).status_code
        == 401
    )
    assert (
        client.post(
            PREFIX + "/auth/login",
            json={
                "email": "owner@example.com",
                "password": PASSWORD,
            },
        ).status_code
        == 200
    )


def test_auth_rejects_disabled_user_and_cross_origin(client):
    owner = register(client)
    assert (
        client.post(PREFIX + "/auth/logout", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert client.get(PREFIX + "/auth/me").status_code == 200
    with client.app.state.session_factory.begin() as db:
        db.get(User, UUID(owner["user"]["id"])).disabled = True
    assert client.get(PREFIX + "/auth/me").status_code == 401
    assert client.post(PREFIX + "/auth/refresh").status_code == 401
    assert (
        client.post(
            PREFIX + "/auth/login",
            json={
                "email": "owner@example.com",
                "password": PASSWORD,
            },
        ).status_code
        == 401
    )


def test_registration_validation_and_uniqueness(client):
    register(client)
    assert (
        client.post(
            PREFIX + "/auth/register",
            json={
                "email": "OWNER@example.com",
                "password": PASSWORD,
                "display_name": "小画家",
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            PREFIX + "/auth/register",
            json={
                "email": "bad",
                "password": "short",
                "display_name": " ",
            },
        ).status_code
        == 422
    )
    client.app.state.settings.allow_registration = False
    assert (
        client.post(
            PREFIX + "/auth/register",
            json={
                "email": "another@example.com",
                "password": PASSWORD,
                "display_name": "小画家",
            },
        ).status_code
        == 403
    )


def test_project_members_roles_and_isolation(client):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    member = register(client, "member@example.com")
    outsider = register(client, "outsider@example.com")
    url = f"{PREFIX}/projects/{project['id']}"
    assert client.get(url).status_code == 404
    assert client.get(f"{PREFIX}/episodes/{episode['id']}").status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            url + "/members", json={"email": "member@example.com", "role": "viewer"}
        ).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.get(url).status_code == 200
    assert client.get(url + "/access").json()["role"] == "viewer"
    assert len(client.get(PREFIX + "/projects").json()) == 1
    assert (
        client.post(url + "/episodes", json={"episode_no": 2, "title": "第二集"}).status_code == 403
    )
    assert (
        client.patch(f"{PREFIX}/episodes/{episode['id']}", json={"title": "越权"}).status_code
        == 403
    )
    assert (
        client.post(
            url + "/members", json={"email": outsider["user"]["email"], "role": "editor"}
        ).status_code
        == 403
    )
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    member_url = url + "/members/" + member["user"]["id"]
    assert client.patch(member_url, json={"role": "editor"}).status_code == 200
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert (
        client.patch(f"{PREFIX}/episodes/{episode['id']}", json={"title": "新标题"}).status_code
        == 200
    )
    assert client.patch(url, json={"budget_limit": "50"}).status_code == 403
    assert client.get(url + "/audit-events").status_code == 403
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert len(client.get(url + "/audit-events").json()) >= 3
    assert client.delete(member_url).status_code == 204
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.get(url).status_code == 404


def test_archive_restore_and_active_workflow_guard(client):
    register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    purl = f"{PREFIX}/projects/{project['id']}"
    eurl = f"{PREFIX}/episodes/{episode['id']}"
    assert client.patch(eurl, json={"archived": True}).status_code == 200
    assert (
        client.post(
            eurl + "/script-runs", json={"story": "你好"}, headers={"Idempotency-Key": "one"}
        ).status_code
        == 409
    )
    assert client.patch(eurl, json={"archived": False}).status_code == 200
    assert client.patch(purl, json={"archived": True}).status_code == 200
    assert (
        client.post(purl + "/episodes", json={"episode_no": 2, "title": "不允许"}).status_code
        == 409
    )
    assert client.patch(purl, json={"name": "不允许"}).status_code == 409
    assert client.patch(purl, json={"archived": False}).status_code == 200
    response = client.post(
        eurl + "/script-runs", json={"story": "你好"}, headers={"Idempotency-Key": "one"}
    )
    assert response.status_code == 202
    assert len(client.get(eurl + "/runs").json()) == 1
    assert client.patch(purl, json={"archived": True}).status_code == 409
    assert client.patch(eurl, json={"title": "进行中不能改"}).status_code == 409
    assert client.patch(purl, json={"name": None}).status_code == 422


def test_canvas_revision_validation_and_scope(client, episode):
    url = f"{PREFIX}/episodes/{episode['id']}/canvas"
    initial = client.get(url).json()
    assert initial["layout"]["revision"] == 0
    assert len(initial["nodes"]) == 1
    body = {
        "expected_revision": 0,
        "positions": {
            initial["nodes"][0]["id"]: {"x": 20, "y": 45},
        },
        "viewport": {"x": 3, "y": 4, "zoom": 0.8},
    }
    saved = client.put(url + "/layout", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert client.get(url).json()["layout"]["positions"] == body["positions"]
    assert client.put(url + "/layout", json=body).status_code == 409
    body["expected_revision"] = 1
    body["positions"]["episode:foreign"] = {"x": 1, "y": 2}
    assert client.put(url + "/layout", json=body).status_code == 422
    body["positions"].pop("episode:foreign")
    body["viewport"]["zoom"] = 100
    assert client.put(url + "/layout", json=body).status_code == 422


def test_bearer_logout_without_refresh_cookie_revokes_session(client):
    register(client)
    client.cookies.clear()
    assert client.get(PREFIX + "/auth/me").status_code == 200
    assert client.post(PREFIX + "/auth/logout").status_code == 204
    assert client.get(PREFIX + "/auth/me").status_code == 401
