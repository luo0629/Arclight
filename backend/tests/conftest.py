import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from arclight.core.config import Settings
from arclight.main import create_app


@pytest.fixture(autouse=True)
def historical_content_registry(request, monkeypatch):
    """0018 seeds predate the DAG registry; activate it once guards reach head.

    Only explicit historical-schema fixtures use this adapter. All ordinary
    API/workflow tests exercise the real current graph writes without patches.
    """
    if request.node.get_closest_marker("legacy_schema") is None:
        return
    from arclight.api import content
    from arclight.application import artifacts, content_dependencies

    def after_migration(function):
        def seeded(db, *args, **kwargs):
            if inspect(db.get_bind()).has_table("dependency_invalidations"):
                return function(db, *args, **kwargs)
            return None

        return seeded

    for module, name in (
        (content, "register_content"),
        (content, "review_registry"),
        (artifacts, "register_content"),
        (content_dependencies, "review_registry"),
    ):
        monkeypatch.setattr(module, name, after_migration(getattr(module, name)))


@pytest.fixture
def settings(tmp_path):
    test_url = os.getenv("ARCLIGHT_TEST_DATABASE_URL")
    if test_url:
        from sqlalchemy.engine import make_url

        from arclight.workflows.checkpoints import open_checkpointer

        if make_url(test_url).database != "arclight_test":
            raise ValueError("集成测试只允许连接名为 arclight_test 的专用数据库")
        schema = "arclight_test_" + uuid4().hex
        engine = create_engine(test_url)
        with engine.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine.dispose()
        scoped_url = (
            make_url(test_url)
            .update_query_dict({"options": f"-csearch_path={schema}"})
            .render_as_string(hide_password=False)
        )
        configuration = Settings(
            _env_file=None,
            environment="test",
            database_url=scoped_url,
            checkpoint_url=scoped_url.replace("postgresql+psycopg://", "postgresql://"),
            provider="mock",
            api_token="test-token",
        )
        with open_checkpointer(configuration) as saver:
            saver.setup()
        return configuration
    return Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite:///{(tmp_path / 'business.db').as_posix()}",
        checkpoint_url=f"sqlite:///{(tmp_path / 'checkpoints.db').as_posix()}",
        provider="mock",
        api_token="test-token",
    )


@pytest.fixture
def migrated(settings, request):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    # Seed legacy/corrupt snapshots BEFORE 0019 installs immutability. Tests that
    # inspect a historical schema must reflect it rather than query later columns.
    # This marker is test-only; ordinary business tests always start at head.
    target = "0018" if request.node.get_closest_marker("legacy_schema") else "head"
    command.upgrade(config, target)
    return config


@pytest.fixture
def protect_history(migrated):
    """Install current guards after an explicitly marked legacy fixture is prepared."""

    def install():
        command.upgrade(migrated, "head")

    return install


@pytest.fixture
def client(settings, migrated):
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer test-token"
        yield client


@pytest.fixture
def episode(client):
    project = client.post("/api/v1/projects", json={"name": "测试漫剧"})
    assert project.status_code == 201, project.text
    response = client.post(
        f"/api/v1/projects/{project.json()['id']}/episodes",
        json={
            "episode_no": 1,
            "title": "雨夜",
            "target_duration_ms": 30000,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
