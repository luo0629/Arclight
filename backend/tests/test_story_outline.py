import copy
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, select

from arclight.application.runtime import execute_run
from arclight.domain.enums import RunStatus
from arclight.persistence.models import Project, ProjectMember, Script, WorkflowRun
from arclight.providers.text import MockTextProvider


def path(episode):
    return f"/api/v1/episodes/{episode['id']}"


def start(client, episode, body=None, key="outline-test"):
    return client.post(
        path(episode) + "/story-outline-runs",
        json=body or {"story": "雨夜，阿青捡到信。"},
        headers={"Idempotency-Key": key},
    )


def execute(client, settings, run_id, revision=0):
    execute_run(client.app.state.session_factory, settings, UUID(run_id), revision)


def generate(client, episode, settings, body=None, key="outline-test"):
    response = start(client, episode, body, key)
    assert response.status_code == 202, response.text
    execute(client, settings, response.json()["run_id"])
    rows = client.get(path(episode) + "/story-outlines").json()
    assert rows, client.get(f"/api/v1/runs/{response.json()['run_id']}").text
    return client.get(path(episode) + "/story-outlines/" + rows[0]["id"]).json()


def save(client, episode, row, content=None, key="save-test"):
    return client.post(
        path(episode) + "/story-outlines",
        json={"base_version_id": row["id"], "content": content or row["content"]},
        headers={"Idempotency-Key": key},
    )


def confirm(client, episode, row):
    return client.post(path(episode) + "/story-outlines/" + row["id"] + "/confirm")


def derive(client, episode, row, key="writer"):
    return client.post(
        path(episode) + "/script-runs",
        json={"outline_version_id": row["id"]},
        headers={"Idempotency-Key": key},
    )


def test_generation_defaults_exact_source_independence_and_replay(
    client, episode, settings, monkeypatch
):
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).story_settings = {
            "target_episode_count": 3,
            "default_episode_duration_ms": 45000,
        }
    story = "  雨夜，阿青捡到信。\r\n"
    body = {"story": story}
    calls = []
    original = MockTextProvider.outline

    def provider(self, source):
        calls.append(source)
        return original(self, source)

    monkeypatch.setattr(MockTextProvider, "outline", provider)
    accepted = start(client, episode, body).json()
    assert start(client, episode, body).json()["run_id"] == accepted["run_id"]
    assert start(client, episode, {"story": "不同"}).status_code == 409
    assert start(client, episode, key="busy").json()["error"]["code"] == "EPISODE_BUSY"
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).story_settings = {"target_episode_count": 1}
    execute(client, settings, accepted["run_id"])
    rows = client.get(path(episode) + "/story-outlines").json()
    assert len(rows) == 1 and rows[0]["version_no"] == 1
    assert "source_snapshot" not in rows[0] and "content" not in rows[0]
    row = client.get(path(episode) + "/story-outlines/" + rows[0]["id"]).json()
    assert row["content"]["target_episode_count"] == 3
    assert row["content"]["episode_duration_ms"] == 45000
    assert [x["episode_no"] for x in row["content"]["episodes"]] == [1, 2, 3]
    assert row["source_snapshot"]["story"] == story
    assert row["source_snapshot"]["story_sha256"] == hashlib.sha256(story.encode()).hexdigest()
    assert row["status"] == "DRAFT" and row["confirmed_by"] is None
    with client.app.state.session_factory.begin() as db:
        assert db.scalar(select(func.count()).select_from(Script)) == 0
        run = db.get(WorkflowRun, UUID(accepted["run_id"]))
        run.status, run.revision = RunStatus.QUEUED, 0

    def forbidden(*args):
        raise AssertionError("persisted result must bypass provider")

    monkeypatch.setattr("arclight.application.story_outline.build_provider", forbidden)
    execute(client, settings, accepted["run_id"])
    assert len(calls) == 1
    assert client.get(path(episode) + "/story-outlines/" + row["id"]).json() == row
    assert client.put(path(episode) + "/story-outlines/" + row["id"], json={}).status_code == 405


def test_edit_confirm_and_writer_fixed_version_across_project_episodes(
    client, episode, settings, monkeypatch
):
    row = generate(
        client,
        episode,
        settings,
        {"story": "原始故事", "target_episode_count": 2, "episode_duration_ms": 30000},
    )
    assert derive(client, episode, row).json()["error"]["code"] == "OUTLINE_NOT_CONFIRMED"
    content = copy.deepcopy(row["content"])
    content["episodes"][0]["goal"] = "明确的一集目标"
    edited = save(client, episode, row, content).json()
    assert edited["base_version_id"] == row["id"] and edited["version_no"] == 2
    assert edited["source_snapshot"] == row["source_snapshot"]
    assert save(client, episode, row, content).json()["id"] == edited["id"]
    assert save(client, episode, row).status_code == 409
    assert confirm(client, episode, edited).json()["status"] == "APPROVED"
    assert confirm(client, episode, edited).json()["status"] == "APPROVED"
    writer = derive(client, episode, edited)
    assert writer.status_code == 202, writer.text
    assert derive(client, episode, edited).json()["run_id"] == writer.json()["run_id"]
    # Another episode may revise the shared plan while the first writer is queued.
    second = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "第二集"},
    ).json()
    changed = copy.deepcopy(content)
    changed["episodes"][0]["goal"] = "后来改动"
    newer = save(client, second, edited, changed, "new-version").json()
    assert newer["status"] == "DRAFT" and newer["version_no"] == 3
    assert client.get(path(second) + "/story-outlines").json()[0]["id"] == newer["id"]
    observed = []
    original = MockTextProvider.script

    def writer_provider(self, *args, **kwargs):
        observed.append(kwargs["outline_context"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MockTextProvider, "script", writer_provider)
    execute(client, settings, writer.json()["run_id"])
    assert observed[0]["version_id"] == edited["id"]
    assert observed[0]["episode"]["goal"] == "明确的一集目标"
    script = client.get(path(episode) + "/scripts").json()[0]
    assert script["content_json"]["summary"] == "明确的一集目标"
    history = client.get(path(episode) + "/scripts/" + script["id"] + "/comparison").json()
    assert history["target"]["outline_version"] == {
        "id": edited["id"],
        "version_no": 2,
        "episode_no": 1,
    }
    assert client.get(path(episode) + "/story-outlines/" + row["id"]).json() == row
    second_writer = derive(client, second, edited, "second-writer")
    assert second_writer.status_code == 202
    with client.app.state.session_factory() as db:
        inputs = db.get(WorkflowRun, UUID(second_writer.json()["run_id"])).input_json
        assert inputs["outline_context"]["episode"]["episode_no"] == 2
        assert inputs["story"] == "原始故事"
        assert "_text_service" not in json.dumps(edited)


def test_import_and_analysis_sources_remain_exact(client, episode, settings):
    from test_story_sources import source_payload

    source = client.post(
        path(episode) + "/story-sources", json=source_payload("原始信件\r\n".encode())
    ).json()
    row = generate(client, episode, settings, {"source_id": source["id"]})
    assert row["source_snapshot"]["source_id"] == source["id"]
    assert row["source_snapshot"]["source_sha256"] == source["sha256"]
    analysis = client.post(
        path(episode) + "/story-analysis-runs",
        json={"source_id": source["id"]},
        headers={"Idempotency-Key": "analysis"},
    ).json()
    execute(client, settings, analysis["run_id"])
    extracted = client.get(path(episode) + "/story-analyses").json()[0]
    row2 = generate(client, episode, settings, {"analysis_id": extracted["id"]}, "from-analysis")
    assert row2["source_snapshot"]["analysis_id"] == extracted["id"]
    assert row2["source_snapshot"]["analysis_version_no"] == 1
    assert row2["source_snapshot"]["story"] == "原始信件\r\n"
    assert row2["source_snapshot"]["source_sha256"] == source["sha256"]
    assert (
        client.get(path(episode) + "/story-outlines?limit=1&offset=1").json()[0]["id"] == row["id"]
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"story": " "},
        {"story": "a" * 20001},
        {"story": "a", "source_id": str(uuid4())},
        {"source_id": "invalid"},
        {"story": "a", "target_episode_count": 0},
        {"story": "a", "target_episode_count": True},
        {"story": "a", "episode_duration_ms": 2000},
        {"story": "a", "extra": 1},
    ],
)
def test_rejects_invalid_requests(client, episode, body):
    response = client.post(
        path(episode) + "/story-outline-runs", json=body, headers={"Idempotency-Key": "invalid"}
    )
    assert response.status_code == 422, response.text
    assert client.get(path(episode) + "/runs").json() == []


@pytest.mark.parametrize("failure", ["provider", "count", "duration", "numbering", "field"])
def test_failed_generation_sanitized_retry_and_contract_gate(
    client, episode, settings, monkeypatch, caplog, failure
):
    original = MockTextProvider.outline

    def broken(self, source):
        if failure == "provider":
            raise RuntimeError("secret-and-private-text")
        result = original(self, source).model_dump()
        if failure == "count":
            result["target_episode_count"] = 2
        elif failure == "duration":
            result["episode_duration_ms"] = 31000
        elif failure == "numbering":
            result["episodes"][0]["episode_no"] = 2
        else:
            result["episodes"][0]["ending_hook"] = " "
        return result

    monkeypatch.setattr(MockTextProvider, "outline", broken)
    response = start(client, episode).json()
    execute(client, settings, response["run_id"])
    failed = client.get(f"/api/v1/runs/{response['run_id']}").json()
    assert failed["status"] == "FAILED"
    assert client.get(path(episode) + "/story-outlines").json() == []
    assert (
        "secret-and-private-text"
        not in caplog.text + client.get(f"/api/v1/runs/{response['run_id']}/events").text
    )
    execute(client, settings, response["run_id"])
    monkeypatch.setattr(MockTextProvider, "outline", original)
    retried = client.post(f"/api/v1/runs/{response['run_id']}/retry")
    assert retried.status_code == 202, retried.text
    execute(client, settings, response["run_id"], failed["revision"] + 1)
    assert client.get(f"/api/v1/runs/{response['run_id']}").json()["status"] == "SUCCEEDED"


def test_missing_foreign_sources_cross_project_plan_and_writer_mismatch(client, episode, settings):
    assert start(client, episode, {"source_id": str(uuid4())}).status_code == 404
    assert start(client, episode, {"analysis_id": str(uuid4())}).status_code == 404
    row = generate(client, episode, settings)
    assert confirm(client, episode, row).status_code == 200
    second = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "集号不匹配"},
    ).json()
    assert derive(client, second, row).json()["error"]["code"] == "OUTLINE_EPISODE_MISMATCH"
    project = client.post("/api/v1/projects", json={"name": "另一项目"}).json()
    other = client.post(
        f"/api/v1/projects/{project['id']}/episodes", json={"episode_no": 1, "title": "不同项目"}
    ).json()
    assert derive(client, other, row).status_code == 404
    assert client.get(path(other) + "/story-outlines/" + row["id"]).status_code == 404
    assert save(client, other, row).status_code == 404
    assert confirm(client, other, row).status_code == 404
    assert client.patch(path(episode), json={"target_duration_ms": 45000}).status_code == 200
    assert derive(client, episode, row).json()["error"]["code"] == "OUTLINE_EPISODE_MISMATCH"


def test_invalid_edit_leaves_approved_version_untouched(client, episode, settings):
    row = generate(client, episode, settings)
    approved = confirm(client, episode, row).json()
    invalid = copy.deepcopy(row["content"])
    invalid["episodes"][0]["goal"] = ""
    assert save(client, episode, row, invalid).status_code == 422
    assert client.get(path(episode) + "/story-outlines/" + row["id"]).json() == approved
    assert save(client, episode, row).json()["status"] == "DRAFT"


def test_cancel_and_shared_key_space(client, episode, settings):
    run = start(client, episode).json()
    assert client.post(f"/api/v1/runs/{run['run_id']}/cancel").status_code == 200
    execute(client, settings, run["run_id"])
    assert client.get(path(episode) + "/story-outlines").json() == []
    response = client.post(
        path(episode) + "/script-runs",
        json={"story": "原文"},
        headers={"Idempotency-Key": "outline-test"},
    )
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_permissions_archived_and_deleted_access(client, settings):
    from test_project_deletion import archive, create_episode, create_project, delete, register

    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    row = generate(client, episode, settings)
    member = register(client, "outline-viewer@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role="viewer"
            )
        )
    assert client.get(path(episode) + "/story-outlines/" + row["id"]).status_code == 200
    assert save(client, episode, row).status_code == 403
    assert start(client, episode).status_code == 403
    assert confirm(client, episode, row).status_code == 403
    with client.app.state.session_factory.begin() as db:
        db.scalar(
            select(ProjectMember).where(ProjectMember.user_id == UUID(member["user"]["id"]))
        ).role = "editor"
    assert save(client, episode, row).status_code == 201
    assert confirm(client, episode, row).status_code == 403
    register(client, "outline-outsider@example.com")
    assert client.get(path(episode) + "/story-outlines").status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    archive(client, project)
    assert client.get(path(episode) + "/story-outlines/" + row["id"]).status_code == 200
    assert save(client, episode, row).status_code == 409
    assert confirm(client, episode, row).status_code == 409
    delete(client, project)
    assert client.get(path(episode) + "/story-outlines/" + row["id"]).status_code == 404


def test_outline_migration_empty_roundtrip_and_queued_guard(client, episode, migrated):
    command.downgrade(migrated, "0008")
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert start(client, episode).status_code == 202
    with pytest.raises(RuntimeError, match="Story outline data exists"):
        command.downgrade(migrated, "0008")


def test_outline_migration_refuses_losing_saved_data(client, episode, settings, migrated):
    generate(client, episode, settings)
    with pytest.raises(RuntimeError, match="Story outline data exists"):
        command.downgrade(migrated, "0008")


def test_provider_receives_fixed_plan_as_data(settings, monkeypatch):
    import httpx

    from arclight.providers.text import OpenAICompatibleTextProvider

    source = {
        "story": "忽略规则的文本只是故事数据。",
        "target_episode_count": 2,
        "episode_duration_ms": 45000,
    }
    expected = MockTextProvider().outline(source)
    received = []
    real_client = httpx.Client

    def handler(request):
        received.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": expected.model_dump_json()}}]}
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert OpenAICompatibleTextProvider(settings).outline(source) == expected
    request = received[0]
    assert source["story"] not in request["messages"][0]["content"]
    assert json.loads(request["messages"][1]["content"]) == source
    assert request["response_format"]["json_schema"]["name"] == "StoryOutlineContent"


def test_postgres_outline_duplicate_submit_and_delivery(client, episode, settings, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from test_concurrency import race, require_postgres

    require_postgres(settings)
    responses = race(
        client.app,
        path(episode) + "/story-outline-runs",
        {"story": "并发大纲"},
        {"Authorization": "Bearer test-token", "Idempotency-Key": "same-key"},
    )
    assert [row.status_code for row in responses] == [202, 202]
    run_id = responses[0].json()["run_id"]
    assert responses[1].json()["run_id"] == run_id
    calls = []
    original = MockTextProvider.outline

    def provider(self, source):
        calls.append(source)
        return original(self, source)

    monkeypatch.setattr(MockTextProvider, "outline", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: execute(client, settings, run_id), range(2)))
    assert len(calls) == 1
    assert len(client.get(path(episode) + "/story-outlines").json()) == 1


def test_postgres_project_outline_numbering_and_save_idempotency(client, episode, settings):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from test_concurrency import race, require_postgres

    require_postgres(settings)
    other = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "共享项目大纲"},
    ).json()
    runs = [start(client, item, key="project-plan").json()["run_id"] for item in [episode, other]]
    barrier = Barrier(2)

    def worker(run_id):
        barrier.wait(timeout=15)
        execute(client, settings, run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, runs))
    rows = client.get(path(episode) + "/story-outlines").json()
    assert [row["version_no"] for row in rows] == [2, 1]
    assert client.get(path(other) + "/story-outlines").json() == rows
    row = client.get(path(episode) + "/story-outlines/" + rows[0]["id"]).json()
    responses = race(
        client.app,
        path(other) + "/story-outlines",
        {"base_version_id": row["id"], "content": row["content"]},
        {"Authorization": "Bearer test-token", "Idempotency-Key": "same-save"},
    )
    assert [item.status_code for item in responses] == [201, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert responses[0].json()["version_no"] == 3


@pytest.mark.parametrize("operation", ["generate", "save", "confirm"])
def test_postgres_outline_refreshes_stale_episode(client, episode, settings, operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_concurrency import require_postgres

    from arclight.agents.story_outline import StoryOutlineContent
    from arclight.application.story_outline import confirm_outline, save_outline, start_outline
    from arclight.core.errors import DomainError
    from arclight.persistence.models import Episode

    require_postgres(settings)
    row = generate(client, episode, settings)
    loaded, archived = Event(), Event()
    factory = client.app.state.session_factory

    def stale_writer():
        with factory.begin() as db:
            cached = db.get(Episode, UUID(episode["id"]))
            assert cached.status == "DRAFT"
            loaded.set()
            assert archived.wait(10)
            with pytest.raises(DomainError) as error:
                if operation == "generate":
                    start_outline(
                        db,
                        cached.id,
                        "service-owner",
                        {"story": "迟到输入"},
                        "stale",
                        "test",
                        settings,
                    )
                elif operation == "save":
                    save_outline(
                        db,
                        cached.id,
                        "service-owner",
                        UUID(row["id"]),
                        StoryOutlineContent.model_validate(row["content"]),
                        "stale",
                    )
                else:
                    confirm_outline(db, cached.id, "service-owner", UUID(row["id"]))
            return error.value.code

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(stale_writer)
        assert loaded.wait(10)
        with factory.begin() as db:
            db.get(Project, UUID(episode["project_id"]), with_for_update=True)
            db.get(Episode, UUID(episode["id"]), with_for_update=True).status = "ARCHIVED"
        archived.set()
        assert result.result(timeout=15) == "EPISODE_ARCHIVED"
    assert client.get(path(episode) + "/story-outlines/" + row["id"]).json() == row


def test_outline_unique_constraints_have_distinct_names(settings, migrated):
    from sqlalchemy import inspect

    from arclight.persistence.models import StoryOutline
    from arclight.persistence.session import build_engine

    expected = {
        ("project_id", "version_no"): "uq_story_outlines_project_version",
        ("project_id", "save_key"): "uq_story_outlines_project_save_key",
    }
    metadata = {
        tuple(column.name for column in constraint.columns): constraint.name
        for constraint in StoryOutline.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    engine = build_engine(settings)
    try:
        reflected = {
            tuple(row["column_names"]): row["name"]
            for row in inspect(engine).get_unique_constraints("story_outlines")
        }
        for columns, name in expected.items():
            assert metadata[columns] == reflected[columns] == name
    finally:
        engine.dispose()
