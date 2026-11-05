import copy
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_director_workflow import advance

from arclight.agents.script_rewrite import RewriteResult
from arclight.application.runtime import execute_run
from arclight.persistence.models import (
    OutboxEvent,
    Project,
    Script,
    Storyboard,
    WorkflowRun,
)
from arclight.providers.text import MockTextProvider, OpenAICompatibleTextProvider


def prefix(episode):
    return f"/api/v1/episodes/{episode['id']}"


def source(client, episode, version=0):
    content = {
        "title": "精确来源",
        "summary": "阿青拾信，寻找失主。",
        "estimated_duration_ms": 30000,
        "characters": ["阿青"],
        "scenes": [
            {
                "scene_id": str(uuid4()),
                "scene_no": i,
                "title": f"场景{i}",
                "action": f"前缀🙂\n场景{i}的段落\n后缀保持。",
                "notes": "不可改的制作备注",
                "dialogue": [
                    {"line_id": str(uuid4()), "speaker": "阿青", "text": "我想把这封信还给你。"},
                    {"line_id": str(uuid4()), "speaker": "旁白", "text": "雨仍在下。"},
                ],
            }
            for i in range(1, 4)
        ],
    }
    response = client.post(
        prefix(episode) + "/scripts", json={"expected_version_no": version, "content": content}
    )
    assert response.status_code == 201, response.text
    return response.json()


def body(base, target=None, instructions=None):
    return {
        "base_version_id": base["id"],
        "target": target
        or {"scope": "scene", "scene_id": base["content_json"]["scenes"][1]["scene_id"]},
        "instructions": instructions or {"instruction": "强化冲突", "tone": "克制"},
    }


def start(client, episode, payload, key="rewrite"):
    return client.post(
        prefix(episode) + "/script-rewrite-runs",
        json=payload,
        headers={"Idempotency-Key": key},
    )


def scripts(client, episode):
    return client.get(prefix(episode) + "/scripts").json()


def review(client, state, decision="APPROVE"):
    return client.post(
        f"/api/v1/runs/{state['id']}/resume",
        json={
            **state["pending_review"],
            "decision": decision,
            "expected_revision": state["revision"],
        },
    )


@pytest.mark.parametrize("decision,terminal", [("APPROVE", "SUCCEEDED"), ("REJECT", "REJECTED")])
def test_rewrite_only_merges_selected_scene_and_reviews_exact_version(
    client, episode, settings, monkeypatch, decision, terminal
):
    base = source(client, episode)
    source(client, episode, version=1)  # The requested source intentionally is historical.
    payload = body(base, instructions={"dialogue_max_chars": 5, "narration_max_percent": 0})
    original_rewrite = MockTextProvider.rewrite
    calls = []

    def rewrite(self, context):
        calls.append(copy.deepcopy(context))
        return original_rewrite(self, context)

    def forbidden(*args, **kwargs):
        raise AssertionError("rewrite may not call a full writer or director")

    monkeypatch.setattr(MockTextProvider, "rewrite", rewrite)
    monkeypatch.setattr(MockTextProvider, "script", forbidden)
    monkeypatch.setattr(MockTextProvider, "storyboard", forbidden)
    response = start(client, episode, payload)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert start(client, episode, payload).json()["run_id"] == run_id
    assert start(client, episode, body(base)).status_code == 409
    assert start(client, episode, payload, key="busy").json()["error"]["code"] == "EPISODE_BUSY"
    assert (
        client.post(
            prefix(episode) + "/script-runs",
            json={"story": "conflict"},
            headers={"Idempotency-Key": "rewrite"},
        ).status_code
        == 409
    )
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    current = scripts(client, episode)[0]
    assert current["version_no"] == 3 and current["run_id"] == run_id
    before, after = base["content_json"], current["content_json"]
    assert after["scenes"][0] == before["scenes"][0]
    assert after["scenes"][2] == before["scenes"][2]
    assert after["scenes"][1]["notes"] == before["scenes"][1]["notes"]
    assert (
        after["scenes"][1]["dialogue"][0]["line_id"]
        == before["scenes"][1]["dialogue"][0]["line_id"]
    )
    assert len(after["scenes"][1]["dialogue"]) == 1
    assert after["characters"] == before["characters"]
    assert scripts(client, episode)[2]["content_json"] == before
    assert state["pending_review"]["version_id"] == current["id"]
    assert (
        client.post(
            prefix(episode) + f"/scripts/{current['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 409
    )
    history = client.get(prefix(episode) + f"/scripts/{current['id']}/comparison").json()["target"]
    assert history["base_version"]["id"] == base["id"]
    assert history["rewrite"]["target"] == payload["target"]
    assert len(history["rewrite"]["source_sha256"]) == 64
    stale = {
        **state["pending_review"],
        "decision": decision,
        "expected_revision": state["revision"] - 1,
    }
    assert client.post(f"/api/v1/runs/{run_id}/resume", json=stale).status_code == 409
    assert review(client, state, decision).status_code == 202
    assert advance(client, settings, run_id)["status"] == terminal
    assert calls[0]["scenes"][0]["scene_id"] == payload["target"]["scene_id"]
    assert len(calls) == 1
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0


@pytest.mark.parametrize("scope", ["chapter", "paragraph"])
def test_chapter_and_unicode_paragraph_leave_other_fields_exact(client, episode, settings, scope):
    base = source(client, episode)
    scenes = base["content_json"]["scenes"]
    target = (
        {"scope": "chapter", "scene_ids": [row["scene_id"] for row in scenes[:2]]}
        if scope == "chapter"
        else {
            "scope": "paragraph",
            "scene_id": scenes[1]["scene_id"],
            "field": "action",
            "start": 4,
            "end": 10,
        }
    )
    run = start(client, episode, body(base, target)).json()
    assert advance(client, settings, run["run_id"])["status"] == "WAITING_REVIEW"
    rewritten = scripts(client, episode)[0]["content_json"]
    assert rewritten["scenes"][2] == scenes[2]
    if scope == "paragraph":
        with client.app.state.session_factory() as db:
            context = db.get(WorkflowRun, UUID(run["run_id"])).input_json["rewrite_input"][
                "context"
            ]
            assert context["surrounding_text"] == {
                "before": scenes[1]["action"][:4],
                "after": scenes[1]["action"][10:],
            }
        assert rewritten["scenes"][0] == scenes[0]
        changed = rewritten["scenes"][1]
        expected = (
            scenes[1]["action"][:4]
            + "演示改写（Mock）："
            + scenes[1]["action"][4:10]
            + scenes[1]["action"][10:]
        )
        assert changed["action"] == expected
        assert {k: v for k, v in changed.items() if k != "action"} == {
            k: v for k, v in scenes[1].items() if k != "action"
        }
    else:
        assert all(row["action"].startswith("演示改写") for row in rewritten["scenes"][:2])


def test_replay_saved_artifact_does_not_build_provider(client, episode, settings, monkeypatch):
    base = source(client, episode)
    run_id = start(client, episode, body(base)).json()["run_id"]
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
    # Drop only this isolated run's checkpoint to recreate the artifact/checkpoint crash window.
    from arclight.workflows.checkpoints import open_checkpointer

    with open_checkpointer(settings) as saver:
        saver.delete_thread(run_id)
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        saved_agent_state = copy.deepcopy(run.agent_state_json)
        run.status, run.revision, run.pending_review = "QUEUED", 0, None

    def forbidden(*args):
        raise AssertionError("persisted artifact must bypass credentials and provider creation")

    monkeypatch.setattr("arclight.application.script_rewrite.build_provider", forbidden)
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    assert len(scripts(client, episode)) == 2
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, UUID(run_id)).agent_state_json == saved_agent_state


@pytest.mark.parametrize(
    "failure",
    [
        "foreign_scene",
        "extra_scene",
        "foreign_line",
        "new_character",
        "duration",
        "limit",
        "ratio",
        "empty_paragraph",
    ],
)
def test_bad_provider_patch_is_atomic_and_explicitly_retryable(
    client, episode, settings, monkeypatch, failure
):
    base = source(client, episode)
    scenes = base["content_json"]["scenes"]
    target = None
    if failure == "empty_paragraph":
        target = {
            "scope": "paragraph",
            "scene_id": scenes[1]["scene_id"],
            "field": "dialogue",
            "line_id": scenes[1]["dialogue"][0]["line_id"],
            "start": 0,
            "end": 2,
        }
    payload = body(
        base, target, {"instruction": "改写", "dialogue_max_chars": 5, "narration_max_percent": 0}
    )
    good = MockTextProvider.rewrite

    def invalid(self, context):
        result = good(self, context).model_dump()
        if failure == "foreign_scene":
            result["scenes"][0]["scene_id"] = scenes[0]["scene_id"]
        elif failure == "extra_scene":
            result["scenes"].append(copy.deepcopy(result["scenes"][0]))
        elif failure == "foreign_line":
            result["scenes"][0]["dialogue"][0]["line_id"] = scenes[0]["dialogue"][0]["line_id"]
        elif failure == "new_character":
            result["scenes"][0]["dialogue"][0]["speaker"] = "新增角色"
        elif failure == "duration":
            result["estimated_duration_ms"] = 1
        elif failure == "limit":
            result["scenes"][0]["dialogue"][0]["text"] = "超出约定字数上限的对白"
        elif failure == "ratio":
            result["scenes"][0]["dialogue"] = [scenes[1]["dialogue"][1]]
        elif failure == "empty_paragraph":
            result["text"] = " "
        return RewriteResult.model_validate(result)

    monkeypatch.setattr(MockTextProvider, "rewrite", invalid)
    run_id = start(client, episode, payload).json()["run_id"]
    state = advance(client, settings, run_id)
    assert state["status"] == "FAILED", state
    assert len(scripts(client, episode)) == 1
    monkeypatch.setattr(MockTextProvider, "rewrite", good)
    if failure == "empty_paragraph":
        # The full unchanged suffix is longer than the requested max: still must fail.
        assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
        assert advance(client, settings, run_id)["status"] == "FAILED"
    else:
        assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
        assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
        assert len(scripts(client, episode)) == 2


@pytest.mark.parametrize(
    "invalid",
    [
        "foreign",
        "noncontiguous",
        "duplicate",
        "reverse",
        "end",
        "line",
        "whitespace",
        "instructions",
        "bool",
        "extra",
    ],
)
def test_invalid_requests_do_not_queue(client, episode, invalid):
    base = source(client, episode)
    scenes = base["content_json"]["scenes"]
    payload = body(base)
    if invalid == "foreign":
        payload["base_version_id"] = str(uuid4())
    elif invalid in {"noncontiguous", "duplicate", "reverse"}:
        indexes = {"noncontiguous": [0, 2], "duplicate": [0, 0], "reverse": [2, 1]}[invalid]
        payload["target"] = {
            "scope": "chapter",
            "scene_ids": [scenes[i]["scene_id"] for i in indexes],
        }
    elif invalid in {"end", "line", "whitespace"}:
        payload["target"] = {
            "scope": "paragraph",
            "scene_id": scenes[0]["scene_id"],
            "field": "action",
            "start": 0,
            "end": 99999,
        }
        if invalid == "line":
            payload["target"].update(field="dialogue", line_id=scenes[1]["dialogue"][0]["line_id"])
        if invalid == "whitespace":
            payload["target"].update(start=3, end=4)
    elif invalid == "instructions":
        payload["instructions"] = {"instruction": "  "}
    elif invalid == "bool":
        payload["instructions"] = {"dialogue_max_chars": True}
    elif invalid == "extra":
        payload["target"]["asset_id"] = str(uuid4())
    response = start(client, episode, payload)
    assert response.status_code == (404 if invalid == "foreign" else 422), response.text
    assert client.get(prefix(episode) + "/runs").json() == []


def test_permissions_archival_cancel_and_snapshot(client, episode, settings):
    from test_collaboration import register

    # Create as an account so owner/editor/viewer gates are exercised.
    owner = register(client)
    project = client.post("/api/v1/projects", json={"name": "权限"}).json()
    episode = client.post(
        f"/api/v1/projects/{project['id']}/episodes", json={"title": "雨", "episode_no": 1}
    ).json()
    base = source(client, episode)
    member = register(client, "rewrite-member@example.com")
    assert start(client, episode, body(base)).status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    path = f"/api/v1/projects/{project['id']}/members"
    assert (
        client.post(path, json={"email": member["user"]["email"], "role": "viewer"}).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert start(client, episode, body(base)).status_code == 403
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.patch(path + "/" + member["user"]["id"], json={"role": "editor"}).status_code == 200
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    run_id = start(client, episode, body(base)).json()["run_id"]
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        assert run.input_json["rewrite_input"]["source_script"] == base["content_json"]
        db.get(Project, UUID(project["id"])).style_guide = {"new": "does not change snapshot"}
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW"
    assert review(client, state).status_code == 403
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert review(client, state).status_code == 202
    assert advance(client, settings, run_id)["status"] == "SUCCEEDED"
    run2 = start(client, episode, body(base), "cancel").json()["run_id"]
    assert client.post(f"/api/v1/runs/{run2}/cancel").status_code == 200
    execute_run(client.app.state.session_factory, settings, UUID(run2), 0)
    assert len(scripts(client, episode)) == 2
    assert client.patch(prefix(episode), json={"archived": True}).status_code == 200
    assert start(client, episode, body(base), "archived").status_code == 409


def test_provider_json_schema_and_credentials_stay_out_of_public_state(
    client, episode, settings, monkeypatch
):
    base = source(client, episode)
    seen = []

    def respond(request):
        data = json.loads(request.content)
        seen.append(data)
        context = json.loads(data["messages"][1]["content"])
        patch = MockTextProvider().rewrite(context)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": patch.model_dump_json()}}]}
        )

    monkeypatch.setattr(
        "arclight.providers.text.PublicHTTPTransport",
        lambda *args, **kwargs: httpx.MockTransport(respond),
    )
    settings_copy = settings.model_copy(update={"provider": "openai_compatible"})
    with client.app.state.session_factory() as db:
        from arclight.agents.script_rewrite import RewriteRequest
        from arclight.application.script_rewrite import start_rewrite

        run = start_rewrite(
            db,
            UUID(episode["id"]),
            "service-owner",
            RewriteRequest.model_validate(body(base)),
            "http",
            "test",
            settings,
        )
        context = run.input_json["rewrite_input"]["context"]
        patch = OpenAICompatibleTextProvider(settings_copy).rewrite(context)
        assert len(patch.scenes) == 1
    assert seen[0]["response_format"]["json_schema"]["name"] == "RewriteResult"
    assert "_text_service" not in json.dumps(seen)


def test_postgres_rewrite_start_and_delivery_are_serialized(client, episode, settings, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    from test_concurrency import race, require_postgres

    require_postgres(settings)
    base = source(client, episode)
    responses = race(
        client.app,
        prefix(episode) + "/script-rewrite-runs",
        body(base),
        {"Authorization": "Bearer test-token", "Idempotency-Key": "race"},
    )
    assert [row.status_code for row in responses] == [202, 202]
    ids = {row.json()["run_id"] for row in responses}
    assert len(ids) == 1
    run_id = ids.pop()
    original, calls, lock = MockTextProvider.rewrite, [], Lock()

    def generate(self, context):
        with lock:
            calls.append(1)
        return original(self, context)

    monkeypatch.setattr(MockTextProvider, "rewrite", generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda _: execute_run(client.app.state.session_factory, settings, UUID(run_id), 0),
                range(2),
            )
        )
    assert calls == [1]
    assert len(scripts(client, episode)) == 2
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.run_id == UUID(run_id))
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.production_event_id.is_not(None))
            )
            == 1
        )


def test_custom_service_snapshot_is_used_and_never_enters_checkpoint(client, settings, monkeypatch):
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    from test_model_services import account, profile

    from arclight.workflows.checkpoints import open_checkpointer

    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    account(client)
    assert (
        client.put(
            "/api/v1/me/model-services/text",
            json=profile(mode="custom", api_key="rewrite-private-key"),
        ).status_code
        == 200
    )
    project = client.post("/api/v1/projects", json={"name": "改写服务快照"}).json()
    episode = client.post(
        f"/api/v1/projects/{project['id']}/episodes", json={"episode_no": 1, "title": "雨"}
    ).json()
    base = source(client, episode)
    run_id = start(client, episode, body(base)).json()["run_id"]
    assert (
        client.put(
            "/api/v1/me/model-services/text",
            json=profile(
                expected_revision=1, mode="server", model="changed-model", api_key="changed-key"
            ),
        ).status_code
        == 200
    )
    captured = []

    def provider(configuration):
        captured.append(configuration)
        return MockTextProvider()

    monkeypatch.setattr("arclight.application.script_rewrite.build_provider", provider)
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW"
    assert captured[0].llm_model == "text-model"
    assert captured[0].llm_api_key.get_secret_value() == "rewrite-private-key"
    with client.app.state.session_factory() as db:
        saved = db.get(WorkflowRun, UUID(run_id)).input_json
        assert "rewrite-private-key" not in json.dumps(saved)
        assert "_text_service" in saved
    with open_checkpointer(settings) as saver:
        checkpoint = saver.get_tuple({"configurable": {"thread_id": run_id}})
        serialized = str(checkpoint)
        for marker in ("_text_service", "encrypted_key", "rewrite-private-key", "changed-key"):
            assert marker not in serialized
    assert "encrypted_key" not in json.dumps(state)
    events = client.get(f"/api/v1/runs/{run_id}/events").text
    assert "rewrite-private-key" not in events and "encrypted_key" not in events


def test_deleted_project_blocks_rewrite_and_restore_keeps_exact_versions(client, episode):
    from test_project_deletion import archive, delete, restore

    base = source(client, episode)
    project = client.get(f"/api/v1/projects/{episode['project_id']}").json()
    archive(client, project)
    assert start(client, episode, body(base)).json()["error"]["code"] == "PROJECT_ARCHIVED"
    deletion = delete(client, project)
    assert deletion.status_code == 200
    assert start(client, episode, body(base)).status_code == 404
    assert client.get(prefix(episode) + f"/scripts/{base['id']}/comparison").status_code == 404
    assert restore(client, project, deletion.json()["deletion_id"]).status_code == 200
    assert scripts(client, episode)[0]["content_json"] == base["content_json"]
    assert start(client, episode, body(base)).status_code == 409


def test_input_limits_legacy_duration_and_foreign_episode(client, episode, settings):
    base = source(client, episode)
    legacy_content = copy.deepcopy(base["content_json"])
    legacy_content.pop("estimated_duration_ms")
    with client.app.state.session_factory.begin() as db:
        original = db.get(Script, UUID(base["id"]))
        legacy = Script(
            episode_id=original.episode_id,
            run_id=None,
            version_no=original.version_no + 1,
            content_json=legacy_content,
            status=original.status,
        )
        db.add(legacy)
        db.flush()
        legacy_base = {
            **base,
            "id": str(legacy.id),
            "version_no": legacy.version_no,
            "content_json": legacy_content,
        }
    other = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "其他"},
    ).json()
    assert start(client, other, body(base)).status_code == 404
    assert client.post(prefix(episode) + "/script-rewrite-runs", json=body(base)).status_code == 422
    assert start(client, episode, body(base), "x" * 129).status_code == 422
    settings.max_story_chars = 1
    assert start(client, episode, body(base)).json()["error"]["code"] == "REWRITE_INPUT_TOO_LARGE"
    settings.max_story_chars = 20000
    assert (
        start(client, episode, body(legacy_base)).json()["error"]["code"]
        == "SCRIPT_DURATION_REQUIRED"
    )
    assert client.get(prefix(episode) + "/runs").json() == []


def test_postgres_stale_archived_episode_cannot_queue_rewrite(client, episode, settings):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_concurrency import require_postgres

    from arclight.agents.script_rewrite import RewriteRequest
    from arclight.application.script_rewrite import start_rewrite
    from arclight.core.errors import DomainError
    from arclight.persistence.models import Episode

    require_postgres(settings)
    base = source(client, episode)
    factory = client.app.state.session_factory
    loaded, archived = Event(), Event()

    def stale_writer():
        with factory.begin() as db:
            cached = db.get(Episode, UUID(episode["id"]))
            assert cached.status == "DRAFT"
            loaded.set()
            assert archived.wait(10)
            with pytest.raises(DomainError) as error:
                start_rewrite(
                    db,
                    cached.id,
                    "service-owner",
                    RewriteRequest.model_validate(body(base)),
                    "stale",
                    "test",
                    settings,
                )
            return error.value.code

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(stale_writer)
        assert loaded.wait(10)
        with factory.begin() as db:
            db.get(Project, UUID(episode["project_id"]), with_for_update=True)
            db.get(Episode, UUID(episode["id"]), with_for_update=True).status = "ARCHIVED"
        archived.set()
        assert pending.result(timeout=15) == "EPISODE_ARCHIVED"
    assert client.get(prefix(episode) + "/runs").json() == []
