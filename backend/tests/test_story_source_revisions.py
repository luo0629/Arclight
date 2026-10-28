import hashlib
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from test_api_workflow import advance, review
from test_story_analysis import execute as execute_analysis
from test_story_analysis import start as start_analysis
from test_story_outline import confirm, generate
from test_story_sources import source_payload, sources_path

from arclight.persistence.models import (
    AuditEvent,
    StorySource,
    StorySourceBinding,
    StorySourceRevision,
    WorkflowRun,
)


def text_payload(text="  雨夜原稿。\r\n", **changes):
    return {
        "import_key": str(uuid4()),
        "original_name": "原始故事.txt",
        "text": text,
        "source_note": "本人原创",
        "rights_note": "本人拥有使用权",
        "rights_confirmed": True,
        **changes,
    }


def save_text(client, episode, payload=None):
    result = client.post(sources_path(episode) + "/text", json=payload or text_payload())
    assert result.status_code == 201, result.text
    return result.json()


def revise(client, episode, base, **changes):
    return text_payload(base_source_id=base["id"], change_note="修改结尾", **changes)


@pytest.mark.parametrize("text", ["  雨夜原稿。\r\n", "\ufeff  雨夜原稿。\r\n"])
def test_explicit_text_preserves_bytes_rights_and_server_owned_object_reference(
    client, episode, text
):
    data = text_payload(text)
    source = save_text(client, episode, data)
    proof = source["provenance"]
    assert source["text"] == data["text"]
    assert source["encoding"] == "utf-8"
    assert proof["root_source_id"] == source["id"]
    assert proof["parent_source_id"] is None and proof["version_no"] == 1
    assert proof["origin_kind"] == "TEXT" and proof["rights_status"] == "CONFIRMED"
    assert proof["object_ref"] == {
        "backend": "database",
        "key": f"story-sources/{source['id']}/original",
        "sha256": hashlib.sha256(data["text"].encode()).hexdigest(),
        "byte_size": len(data["text"].encode()),
    }
    original = client.get(sources_path(episode) + "/" + source["id"] + "/original")
    assert original.content == data["text"].encode()
    assert "text" not in client.get(sources_path(episode)).json()[0]
    assert client.get(f"/api/v1/episodes/{episode['id']}/runs").json() == []
    for override in [{"object_ref": {"backend": "s3", "key": "other"}}, {"rights_confirmed": 1}]:
        assert (
            client.post(sources_path(episode) + "/text", json={**data, **override}).status_code
            == 422
        )


def test_revision_chain_is_linear_paginated_immutable_and_replay_precedes_conflict(client, episode):
    first = save_text(client, episode)
    payload = revise(client, episode, first, text="第二版")
    second = save_text(client, episode, payload)
    third = save_text(client, episode, revise(client, episode, second, text="第三版"))
    assert second["provenance"]["version_no"] == 2
    assert third["provenance"]["parent_source_id"] == second["id"]
    assert third["provenance"]["root_source_id"] == first["id"]
    assert save_text(client, episode, payload) == second
    assert (
        client.post(
            sources_path(episode) + "/text", json={**payload, "text": "不同请求"}
        ).status_code
        == 409
    )
    stale = client.post(sources_path(episode) + "/text", json=revise(client, episode, first))
    assert stale.json()["error"]["code"] == "SOURCE_VERSION_CONFLICT"
    history = sources_path(episode) + "/" + second["id"] + "/history"
    assert client.get(history + "?limit=1&offset=1").json()[0]["id"] == second["id"]
    assert [row["id"] for row in client.get(history).json()] == [
        third["id"],
        second["id"],
        first["id"],
    ]
    assert client.get(sources_path(episode) + "/" + first["id"]).json() == first
    assert len(client.get(sources_path(episode)).json()) == 3
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(StorySourceRevision)) == 3
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "story_source.revised")
            )
            == 2
        )


def test_file_and_text_revisions_share_metadata_without_replacing_original(client, episode):
    raw = "原始中文\r\n".encode("gb18030")
    first_response = client.post(
        sources_path(episode), json=source_payload(raw, encoding="gb18030")
    )
    assert first_response.status_code == 201
    first = first_response.json()
    second = save_text(client, episode, revise(client, episode, first, text="改写😀\r\n"))
    uploaded = client.post(
        sources_path(episode),
        json=source_payload(
            b"third file\r\n",
            base_source_id=second["id"],
            change_note="上传第三稿",
        ),
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["provenance"]["version_no"] == 3
    assert uploaded.json()["provenance"]["origin_kind"] == "FILE"
    assert first["encoding"] == "gb18030" and second["encoding"] == "utf-8"
    assert client.get(sources_path(episode) + "/" + first["id"] + "/original").content == raw


@pytest.mark.parametrize(
    "change",
    [{"change_note": ""}, {"change_note": "  "}, {"rights_confirmed": False}, {"rights_note": " "}],
)
def test_invalid_revision_is_atomic(client, episode, change):
    first = save_text(client, episode)
    payload = {**revise(client, episode, first), **change}
    assert client.post(sources_path(episode) + "/text", json=payload).status_code == 422
    assert len(client.get(sources_path(episode)).json()) == 1


def test_revision_scope_archive_and_read_permissions(client, episode):
    first = save_text(client, episode)
    other = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "other"},
    ).json()
    assert (
        client.post(sources_path(other) + "/text", json=revise(client, episode, first)).status_code
        == 404
    )
    for suffix in ("/history", "/usages", "/original"):
        assert client.get(sources_path(other) + "/" + first["id"] + suffix).status_code == 404
    client.patch(f"/api/v1/episodes/{episode['id']}", json={"archived": True})
    assert (
        client.post(
            sources_path(episode) + "/text", json=revise(client, episode, first)
        ).status_code
        == 409
    )
    assert client.get(sources_path(episode) + "/" + first["id"] + "/history").status_code == 200


@pytest.mark.parametrize("kind", ["script-runs", "story-analysis-runs", "story-outline-runs"])
@pytest.mark.parametrize("story", ["  雨夜，女孩走进书店。\r\n", "\ufeff  雨夜。\r\n"])
def test_inline_text_is_an_entity_with_undeclared_rights_and_idempotent_binding(
    client, episode, kind, story
):
    path = f"/api/v1/episodes/{episode['id']}/{kind}"
    headers = {"Idempotency-Key": "capture-once"}
    response = client.post(path, headers=headers, json={"story": story})
    assert response.status_code == 202, response.text
    assert client.post(path, headers=headers, json={"story": story}).json() == response.json()
    sources = client.get(sources_path(episode)).json()
    assert len(sources) == 1 and sources[0]["provenance"]["rights_status"] == "UNDECLARED"
    source = client.get(sources_path(episode) + "/" + sources[0]["id"]).json()
    assert source["text"] == story and source["provenance"]["origin_kind"] == "INLINE"
    with client.app.state.session_factory() as db:
        binding = db.scalar(select(StorySourceBinding))
        assert str(binding.run_id) == response.json()["run_id"]
        run = db.get(WorkflowRun, binding.run_id)
        snapshot = run.input_json.get("source_snapshot", run.input_json)
        assert snapshot["source_revision"]["source_id"] == source["id"]
        assert snapshot["story"] == story


def test_revising_while_queued_does_not_change_input_approved_content_or_retries(
    client, episode, settings
):
    first = save_text(client, episode)
    started = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        headers={"Idempotency-Key": "fixed-source"},
        json={"source_id": first["id"]},
    )
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    with client.app.state.session_factory() as db:
        frozen = db.get(WorkflowRun, UUID(run_id)).input_json.copy()
    second = save_text(client, episode, revise(client, episode, first, text="全新的另一稿"))
    state = advance(client, settings, run_id)
    script_id = state["pending_review"]["version_id"]
    assert review(client, state).status_code == 202
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, UUID(run_id)).input_json == frozen
    script = client.get(f"/api/v1/episodes/{episode['id']}/scripts").json()[0]
    assert script["id"] == script_id and script["status"] == "APPROVED"
    assert frozen["story"] == first["text"] and frozen["source_revision"]["version_no"] == 1
    uses = client.get(sources_path(episode) + "/" + first["id"] + "/usages").json()
    assert len(uses) == 1 and uses[0]["script_id"] == script_id
    assert client.get(sources_path(episode) + "/" + second["id"] + "/usages").json() == []
    assert "input_json" not in str(uses) and "_text_service" not in str(uses)


def test_analysis_outline_and_cross_episode_writer_retain_exact_source(client, episode, settings):
    first = save_text(client, episode)
    started = start_analysis(client, episode, {"source_id": first["id"]})
    execute_analysis(client, settings, started.json()["run_id"])
    analysis = client.get(f"/api/v1/episodes/{episode['id']}/story-analyses").json()[0]
    save_text(client, episode, revise(client, episode, first, text="与旧提炼不同的修订"))
    outline = generate(client, episode, settings, {"analysis_id": analysis["id"]})
    assert outline["source_snapshot"]["source_revision"]["source_id"] == first["id"]
    assert confirm(client, episode, outline).status_code == 200
    writer = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        headers={"Idempotency-Key": "writer"},
        json={"outline_version_id": outline["id"]},
    )
    assert writer.status_code == 202, writer.text
    uses = client.get(sources_path(episode) + "/" + first["id"] + "/usages").json()
    assert {row["workflow_type"] for row in uses} == {
        "story_analysis",
        "story_outline",
        "script_storyboard",
    }
    assert (
        client.get(sources_path(episode) + "/" + first["id"] + "/usages?limit=1&offset=1").json()[
            0
        ]["outline_id"]
        == outline["id"]
    )


@pytest.mark.legacy_schema
def test_0011_backfills_legacy_file_without_touching_bytes_or_run_input(client, episode, migrated):
    command.downgrade(migrated, "0010")
    raw = b"\xef\xbb\xbflegacy\r\n"
    with client.app.state.session_factory.begin() as db:
        source = StorySource(
            episode_id=UUID(episode["id"]),
            import_no=1,
            import_key=uuid4(),
            request_fingerprint="a" * 64,
            original_name="legacy.txt",
            source_type="TXT",
            media_type="text/plain",
            encoding="utf-8-sig",
            byte_size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            raw_bytes=raw,
            text="legacy\r\n",
            source_note="old origin",
            rights_note="old rights",
            created_by="service-owner",
        )
        db.add(source)
        db.flush()
        source_id = source.id
        run = WorkflowRun(
            episode_id=UUID(episode["id"]),
            idempotency_key="legacy",
            request_fingerprint="b" * 64,
            trace_id="legacy",
            input_json={"story": "legacy\r\n", "source_id": str(source_id)},
        )
        db.add(run)
        db.flush()
        run_id = run.id
    command.upgrade(migrated, "0018")
    restored = client.get(sources_path(episode) + "/" + str(source_id)).json()
    assert restored["provenance"]["version_no"] == 1
    assert restored["rights_note"] == "old rights"
    assert client.get(sources_path(episode) + "/" + str(source_id) + "/original").content == raw
    assert client.get(sources_path(episode) + "/" + str(source_id) + "/usages").json()[0][
        "run_id"
    ] == str(run_id)
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, run_id).input_json == {
            "story": "legacy\r\n",
            "source_id": str(source_id),
        }
    with pytest.raises(RuntimeError, match="Source provenance exists"):
        command.downgrade(migrated, "0010")
    # Verify the current schema after the historical preservation assertions.
    command.upgrade(migrated, "head")
    command.check(migrated)


def test_offline_source_revision_downgrade_requires_online_provenance_check():
    from pathlib import Path

    config = Config(
        str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=StringIO()
    )
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    with pytest.raises(RuntimeError, match="online provenance check"):
        command.downgrade(config, "0011:0010", sql=True)


def test_concurrent_revisions_have_one_winner_on_postgres(client, episode, settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL project write-gate concurrency")
    first = save_text(client, episode)
    payloads = [revise(client, episode, first, text=f"revision {index}") for index in range(2)]

    def submit(payload):
        return client.post(sources_path(episode) + "/text", json=payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, payloads))
    assert sorted(result.status_code for result in results) == [201, 409]
    assert len(client.get(sources_path(episode)).json()) == 2


@pytest.mark.parametrize("text", [" ", "text\x00value", "story�"])
def test_text_rejects_invalid_plain_text_without_partial_source(client, episode, text):
    assert client.post(sources_path(episode) + "/text", json=text_payload(text)).status_code == 422
    assert client.get(sources_path(episode)).json() == []
