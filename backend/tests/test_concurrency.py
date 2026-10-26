from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from test_collaboration import register

from arclight.main import create_app
from arclight.providers.text import MockTextProvider


def require_postgres(settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("并发一致性必须使用真实 PostgreSQL 行锁验证")


def race(app, path, body=None, headers=None, cookie=None):
    barrier = Barrier(2)

    def request(_):
        with TestClient(create_app(app.state.settings), backend="asyncio") as connection:
            if cookie:
                connection.cookies.set("arclight_refresh", cookie, path="/api/v1/auth")
            barrier.wait(timeout=15)
            method = connection.put if path.endswith("/layout") else connection.post
            return method(path, json=body, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(request, range(2)))


def test_postgres_refresh_rotation_allows_only_one_consumer(client, settings):
    require_postgres(settings)
    register(client)
    cookie = client.cookies.get("arclight_refresh")
    responses = race(client.app, "/api/v1/auth/refresh", cookie=cookie)
    assert sorted(row.status_code for row in responses) == [200, 401]


def test_postgres_canvas_first_save_has_single_winner(client, episode, settings):
    require_postgres(settings)
    path = f"/api/v1/episodes/{episode['id']}/canvas/layout"
    body = {"expected_revision": 0, "positions": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}}
    responses = race(client.app, path, body, {"Authorization": "Bearer test-token"})
    assert sorted(row.status_code for row in responses) == [200, 409]
    assert client.get(path.removesuffix("/layout")).json()["layout"]["revision"] == 1


def test_postgres_manual_version_has_single_winner(client, episode, settings):
    require_postgres(settings)
    path = f"/api/v1/episodes/{episode['id']}/scripts"
    body = {"expected_version_no": 0, "content": MockTextProvider().script("雨天", {}).model_dump()}
    responses = race(client.app, path, body, {"Authorization": "Bearer test-token"})
    assert sorted(row.status_code for row in responses) == [201, 409]
    assert len(client.get(path).json()) == 1


def test_postgres_deletion_refreshes_a_waiting_stale_writer(client, settings, episode):
    from datetime import timedelta
    from threading import Event
    from uuid import UUID, uuid4

    from arclight.application.commands import require_project
    from arclight.core.errors import DomainError
    from arclight.persistence.base import utcnow
    from arclight.persistence.models import Project

    require_postgres(settings)
    project_id = UUID(episode["project_id"])
    factory = client.app.state.session_factory
    loaded, attempt = Event(), Event()

    def stale_writer():
        with factory.begin() as db:
            stale = db.get(Project, project_id)
            loaded.set()
            assert attempt.wait(10)
            try:
                require_project(db, project_id, "service-owner", owner=True)
            except DomainError as error:
                return error.status_code
            stale.name = "must-not-write"
            return 200

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(stale_writer)
        assert loaded.wait(10)
        with factory.begin() as db:
            project = db.get(Project, project_id, with_for_update=True)
            project.archived = True
            project.deleted_at = utcnow()
            project.restore_until = utcnow() + timedelta(days=30)
            project.deletion_id = uuid4()
            attempt.set()
        assert pending.result(timeout=15) == 404
    with factory() as db:
        assert db.get(Project, project_id).name != "must-not-write"


def test_postgres_concurrent_deletes_share_one_receipt(client, settings, episode):
    from test_project_deletion import archive

    require_postgres(settings)
    path = f"/api/v1/projects/{episode['project_id']}"
    project = client.get(path).json()
    archive(client, project)
    barrier = Barrier(2)

    def remove(_):
        with TestClient(create_app(settings)) as connection:
            barrier.wait(timeout=15)
            return connection.request(
                "DELETE",
                path,
                json={"confirm_name": project["name"]},
                headers={"Authorization": "Bearer test-token"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(remove, range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    receipt = responses[0].json()
    assert (
        client.post(path + "/restore", json={"deletion_id": receipt["deletion_id"]}).status_code
        == 200
    )
    events = client.get(path + "/audit-events").json()
    assert sum(event["action"] == "project.deleted" for event in events) == 1


def test_postgres_delete_and_unarchive_cannot_both_succeed(client, settings, episode):
    from test_project_deletion import archive

    require_postgres(settings)
    path = f"/api/v1/projects/{episode['project_id']}"
    project = client.get(path).json()
    archive(client, project)
    barrier = Barrier(2)

    def change(method):
        with TestClient(create_app(settings)) as connection:
            barrier.wait(timeout=15)
            return connection.request(
                method,
                path,
                json={"confirm_name": project["name"]}
                if method == "DELETE"
                else {"archived": False},
                headers={"Authorization": "Bearer test-token"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        deleted, unarchived = list(pool.map(change, ["DELETE", "PATCH"]))
    assert (deleted.status_code, unarchived.status_code) in [(200, 404), (409, 200)]
