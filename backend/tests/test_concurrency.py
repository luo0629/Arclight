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
