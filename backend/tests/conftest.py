import os
import socket
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from arclight.core.config import Settings
from arclight.main import create_app


@contextmanager
def serve_asgi(app):
    """Exercise the production streaming ASGI app over a real loopback connection."""
    import uvicorn

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "Isolated ASGI service failed to start"
            yield address
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "Isolated ASGI service failed to stop"


@pytest.fixture
def gateway_server(client):
    import httpx

    from arclight.file_gateway import GatewaySettings, create_gateway

    with ExitStack() as stack:

        def start(settings):
            app = create_gateway(
                GatewaySettings(
                    environment="test",
                    file_gateway_api_url="http://test-api",
                    s3_endpoint_url=settings.s3_endpoint_url,
                    file_gateway_secret=settings.file_gateway_secret,
                ),
                authorization_transport=httpx.ASGITransport(app=client.app),
            )
            address = stack.enter_context(serve_asgi(app))
            settings.file_transfer_base_url = address + "/file-transfers"
            return address

        yield start


@pytest.fixture(autouse=True)
def historical_content_registry(request, monkeypatch):
    """0018 seeds predate the DAG registry; activate it once guards reach head.

    Only explicit historical-schema fixtures use this adapter. All ordinary
    API/workflow tests exercise the real current graph writes without patches.
    """
    if request.node.get_closest_marker("legacy_schema") is None:
        return
    import legacy_story_sources

    from arclight.api import rights
    from arclight.api import story_sources as source_api
    from arclight.application import commands, story_analysis, story_outline, story_sources

    # Explicit historical-schema fixtures exercise the frozen database writer.
    # Ordinary/current-schema tests always use real private objects and current code.
    for module in (source_api, rights, commands, story_analysis, story_outline, story_sources):
        if hasattr(module, "StorySource"):
            monkeypatch.setattr(module, "StorySource", legacy_story_sources.StorySource)
        if hasattr(module, "source_text"):
            monkeypatch.setattr(module, "source_text", lambda db, settings, row: row.text)
    monkeypatch.setattr(source_api, "source_original", lambda db, settings, row: row.raw_bytes)
    monkeypatch.setattr(source_api, "save_source", legacy_story_sources.save_source)
    monkeypatch.setattr(story_sources, "save_source", legacy_story_sources.save_source)
    if hasattr(request.module, "StorySource"):
        monkeypatch.setattr(request.module, "StorySource", legacy_story_sources.StorySource)
    from arclight.api import asset_versions, content
    from arclight.application import artifacts, content_dependencies

    def after_migration(function):
        def seeded(db, *args, **kwargs):
            if inspect(db.get_bind()).has_table("dependency_invalidations"):
                return function(db, *args, **kwargs)
            return None

        return seeded

    from arclight.application import production_versions

    def current_versions(function):
        def wrapped(db, *args, **kwargs):
            if inspect(db.get_bind()).has_table("production_versions"):
                return function(db, *args, **kwargs)
            return None

        return wrapped

    for name in ("capture_text", "capture_media", "bind_output"):
        monkeypatch.setattr(
            production_versions, name, current_versions(getattr(production_versions, name))
        )
    from arclight.application import runtime

    original_settings = runtime.text_settings

    def legacy_settings(db, project_id, run, settings):
        if inspect(db.get_bind()).has_table("production_versions"):
            return original_settings(db, project_id, run, settings)
        return settings

    monkeypatch.setattr(runtime, "text_settings", legacy_settings)

    original_record = asset_versions.record_asset_file

    def record_on_current_schema(db, *args, **kwargs):
        if inspect(db.get_bind()).has_table("stored_files"):
            return original_record(db, *args, **kwargs)
        return None

    monkeypatch.setattr(asset_versions, "record_asset_file", record_on_current_schema)

    for module, name in (
        (content, "register_content"),
        (content, "review_registry"),
        (artifacts, "register_content"),
        (content_dependencies, "review_registry"),
    ):
        monkeypatch.setattr(module, name, after_migration(getattr(module, name)))


@pytest.fixture
def settings(tmp_path):
    objects = tmp_path / "objects"
    objects.mkdir()
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
            asset_storage_path=objects,
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
        asset_storage_path=objects,
    )


@pytest.fixture
def migrated(settings, request):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    config.attributes["storage_settings"] = settings
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
