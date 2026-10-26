import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, select
from test_api_workflow import advance, review, start
from test_collaboration import register

from arclight.domain.project_settings import ProjectStorySettings
from arclight.persistence.models import AuditEvent, Project, WorkflowRun
from arclight.persistence.session import build_engine
from arclight.providers.text import MockTextProvider, OpenAICompatibleTextProvider

BRIEF = {
    "description": "街角旧书店的故事",
    "genre": "温暖悬疑",
    "target_audience": "青少年及家长",
    "content_restrictions": "不出现血腥画面",
    "default_episode_duration_ms": 45001,
    "target_episode_count": 12,
    "episode_plan": "第一集收到信，第二集寻找作者。",
}


def test_create_patch_defaults_and_duration_inheritance(client):
    created = client.post("/api/v1/projects", json={"name": "故事", "story_settings": BRIEF})
    assert created.status_code == 201, created.text
    project = created.json()
    path = f"/api/v1/projects/{project['id']}"
    assert project["story_settings"] == {"schema_version": 1, **BRIEF}
    assert client.get(path).json()["story_settings"] == project["story_settings"]
    assert client.get("/api/v1/projects").json()[0]["story_settings"] == project["story_settings"]
    first = client.post(path + "/episodes", json={"episode_no": 1, "title": "继承"})
    explicit = client.post(
        path + "/episodes",
        json={
            "episode_no": 2,
            "title": "覆盖",
            "target_duration_ms": 60000,
        },
    )
    assert first.status_code == explicit.status_code == 201
    assert first.json()["target_duration_ms"] == 45001
    assert explicit.json()["target_duration_ms"] == 60000
    patch = client.patch(
        path, json={"story_settings": {**BRIEF, "default_episode_duration_ms": 90000}}
    )
    assert patch.status_code == 200
    assert (
        client.get(f"/api/v1/episodes/{first.json()['id']}").json()["target_duration_ms"] == 45001
    )
    third = client.post(path + "/episodes", json={"episode_no": 3, "title": "新默认"})
    assert third.json()["target_duration_ms"] == 90000
    assert (
        client.post(
            path + "/episodes",
            json={
                "episode_no": 4,
                "title": "拒绝空值",
                "target_duration_ms": None,
            },
        ).status_code
        == 422
    )
    # The nested brief is replaced, not deep-merged; omitted fields reset to defaults.
    reset = client.patch(path, json={"story_settings": {}})
    assert reset.json()["story_settings"] == ProjectStorySettings().model_dump()
    with client.app.state.session_factory() as db:
        event = db.scalars(select(AuditEvent).where(AuditEvent.action == "project.updated")).first()
        assert event.details_json == {"fields": ["story_settings"]}
        assert "街角" not in str(event.details_json)


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", "a" * 4001),
        ("genre", "a" * 101),
        ("target_audience", "a" * 501),
        ("content_restrictions", "a" * 4001),
        ("episode_plan", "a" * 4001),
        ("description", None),
        ("default_episode_duration_ms", 2499),
        ("default_episode_duration_ms", 1800001),
        ("default_episode_duration_ms", 30000.5),
        ("default_episode_duration_ms", True),
        ("default_episode_duration_ms", "30000"),
        ("target_episode_count", 0),
        ("target_episode_count", 1001),
        ("target_episode_count", 1.5),
        ("target_episode_count", True),
        ("schema_version", 2),
        ("unknown", "value"),
    ],
)
def test_brief_validation_is_atomic(client, episode, field, value):
    path = f"/api/v1/projects/{episode['project_id']}"
    before = client.get(path).json()
    invalid = {field: value}
    assert (
        client.post(
            "/api/v1/projects", json={"name": "invalid", "story_settings": invalid}
        ).status_code
        == 422
    )
    assert (
        client.patch(path, json={"name": "not saved", "story_settings": invalid}).status_code == 422
    )
    assert client.get(path).json() == before


@pytest.mark.parametrize("duration,count", [(2500, 1), (1800000, 1000)])
def test_brief_boundaries_and_trim(client, duration, count):
    response = client.post(
        "/api/v1/projects",
        json={
            "name": "boundary",
            "story_settings": {
                "description": "  中文简介  ",
                "genre": "a" * 100,
                "target_audience": "a" * 500,
                "content_restrictions": "a" * 4000,
                "episode_plan": "a" * 4000,
                "default_episode_duration_ms": duration,
                "target_episode_count": count,
            },
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["story_settings"]["description"] == "中文简介"


def test_permissions_archival_and_activity(client, episode):
    path = f"/api/v1/projects/{episode['project_id']}"
    assert client.patch(path, json={"story_settings": None}).status_code == 422
    run = start(client, episode).json()
    blocked = client.patch(path, json={"story_settings": BRIEF})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "ACTIVE_WORKFLOW"
    assert client.post(f"/api/v1/runs/{run['run_id']}/cancel").status_code == 200
    assert client.patch(path, json={"archived": True}).status_code == 200
    assert client.patch(path, json={"story_settings": BRIEF}).status_code == 409
    assert client.patch(path, json={"archived": False}).status_code == 200
    assert client.patch(path, json={"story_settings": BRIEF}).status_code == 200


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_only_owner_changes_brief(client, role):
    owner = register(client, "owner-brief@example.com")
    project = client.post("/api/v1/projects", json={"name": "private"}).json()
    path = f"/api/v1/projects/{project['id']}"
    other = register(client, "other-brief@example.com")
    owner_headers = {"Authorization": "Bearer " + owner["access_token"]}
    if role != "outsider":
        assert (
            client.post(
                path + "/members",
                headers=owner_headers,
                json={
                    "email": other["user"]["email"],
                    "role": role,
                },
            ).status_code
            == 201
        )
    response = client.patch(path, json={"story_settings": BRIEF})
    assert response.status_code == (404 if role == "outsider" else 403)
    assert (
        client.get(path, headers=owner_headers).json()["story_settings"]
        == ProjectStorySettings().model_dump()
    )


def test_snapshot_retry_and_history_are_isolated(client, episode, settings, monkeypatch):
    path = f"/api/v1/projects/{episode['project_id']}"
    assert client.patch(path, json={"story_settings": BRIEF}).status_code == 200
    requests = []

    class RecordingProvider(MockTextProvider):
        def script(
            self, story, style, duration_ms=30000, *, story_settings=None, story_context=None
        ):
            requests.append((story_settings, duration_ms))
            if len(requests) == 1:
                raise RuntimeError("transient fake failure")
            return super().script(story, style, duration_ms, story_settings=story_settings)

    monkeypatch.setattr(
        "arclight.application.runtime.build_provider", lambda _: RecordingProvider()
    )
    run_id = start(client, episode, key="brief-snapshot").json()["run_id"]
    failed = advance(client, settings, run_id)
    assert failed["status"] == "FAILED"
    # Failed runs no longer block edits; explicit retry must still use the OLD brief.
    assert (
        client.patch(path, json={"story_settings": {**BRIEF, "genre": "新题材"}}).status_code == 200
    )
    assert start(client, episode, key="brief-snapshot").json()["run_id"] == run_id
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    pending = advance(client, settings, run_id)
    assert pending["status"] == "WAITING_REVIEW"
    assert requests == [({"schema_version": 1, **BRIEF}, 30000)] * 2
    assert review(client, pending).status_code == 202
    pending = advance(client, settings, run_id)
    assert review(client, pending).status_code == 202
    assert advance(client, settings, run_id)["status"] == "SUCCEEDED"
    scripts = client.get(f"/api/v1/episodes/{episode['id']}/scripts").json()
    assert client.patch(path, json={"story_settings": {"genre": "又一题材"}}).status_code == 200
    assert client.get(f"/api/v1/episodes/{episode['id']}/scripts").json() == scripts
    with client.app.state.session_factory() as db:
        assert (
            db.get(WorkflowRun, UUID(run_id)).input_json["story_settings"]["genre"]
            == BRIEF["genre"]
        )


def test_legacy_run_without_brief_does_not_load_current_settings(
    client, episode, settings, monkeypatch
):
    run_id = start(client, episode).json()["run_id"]
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        run.input_json = {
            k: v for k, v in run.input_json.items() if k not in {"story_settings", "story_context"}
        }
        db.get(Project, UUID(episode["project_id"])).story_settings = BRIEF
    calls = []

    class LegacyProvider(MockTextProvider):
        def script(self, story, style, duration_ms=30000):
            calls.append((story, style, duration_ms))
            return super().script(story, style, duration_ms)

    monkeypatch.setattr("arclight.application.runtime.build_provider", lambda _: LegacyProvider())
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
    assert len(calls) == 1


def test_real_adapter_sends_bounded_brief_as_data(settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": MockTextProvider()
                            .script("story", {}, 30000)
                            .model_dump_json(),
                        }
                    }
                ]
            },
        )

    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    OpenAICompatibleTextProvider(settings).script(
        "用户故事", {"description": "水彩"}, 30000, story_settings=BRIEF
    )
    data = json.loads(calls[0]["messages"][1]["content"])
    assert data["story_settings"] == {"schema_version": 1, **BRIEF}
    assert data["target_duration_ms"] == 30000
    assert data["style_guide"] == {"description": "水彩"}
    assert "不出现血腥画面" not in calls[0]["messages"][0]["content"]


def test_0004_migration_preserves_legacy_project_and_guards_downgrade(settings):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "0003")
    engine = build_engine(settings)
    metadata = MetaData()
    metadata.reflect(engine)
    key = uuid4()
    with engine.begin() as db:
        db.execute(
            metadata.tables["projects"]
            .insert()
            .values(
                id=key if engine.dialect.name == "postgresql" else key.hex,
                name="legacy",
                owner_id="service-owner",
                style_guide={"description": "old"},
                output_profile={},
                archived=False,
                budget_limit=0,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    command.upgrade(config, "head")
    with engine.connect() as db:
        row = db.execute(select(Project.__table__)).mappings().one()
        assert row["story_settings"] == {}
        assert row["style_guide"] == {"description": "old"}
        assert (
            ProjectStorySettings.model_validate(row["story_settings"]).default_episode_duration_ms
            == 30000
        )
    command.check(config)
    with engine.begin() as db:
        db.execute(Project.__table__.update().values(story_settings=BRIEF))
    with pytest.raises(RuntimeError, match="back up"):
        command.downgrade(config, "0003")
    with engine.connect() as db:
        assert db.execute(select(Project.story_settings)).scalar_one() == BRIEF
    with engine.begin() as db:
        db.execute(Project.__table__.update().values(story_settings={}))
    command.downgrade(config, "0003")
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()
