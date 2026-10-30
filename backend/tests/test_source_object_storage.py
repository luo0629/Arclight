"""STORE-02: exact originals live outside SQL, including historical migrations."""

import hashlib
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from test_storage_cleanup import gc_setup as gc_setup
from test_storage_cleanup import plan, run_item
from test_story_sources import source_payload, sources_path

from arclight.api import story_sources as source_api
from arclight.application.runtime import execute_run
from arclight.application.source_objects import source_key
from arclight.core.errors import DomainError
from arclight.persistence.models import Episode, StorySource, WorkflowRun
from arclight.storage.objects import ObjectStorage


@pytest.mark.parametrize("kind", ["utf8", "gb18030", "text", "inline"])
def test_originals_are_private_objects_and_sql_contains_metadata_only(
    client, episode, gc_setup, kind
):
    settings, store, _, _ = gc_setup
    raw = b"\xef\xbb\xbfExact original\r\n"
    expected = "Exact original\r\n"
    if kind == "gb18030":
        expected = "原始故事\r\n"
        raw = expected.encode("gb18030")
    path = sources_path(episode)
    if kind in {"utf8", "gb18030"}:
        response = client.post(
            path, json=source_payload(raw, encoding="gb18030" if kind == "gb18030" else "utf-8")
        )
    elif kind == "text":
        expected = "\ufeff  Saved exact text\r\n"
        raw = expected.encode()
        payload = source_payload()
        payload.pop("content_base64")
        response = client.post(path + "/text", json={**payload, "text": expected})
    else:
        expected = "\ufeff  Manual story\r\n"
        raw = expected.encode()
        response = client.post(
            f"/api/v1/episodes/{episode['id']}/script-runs",
            json={"story": expected},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 202, response.text
        source_id = client.get(path).json()[0]["id"]
        response = client.get(path + "/" + source_id)
    assert response.status_code in {200, 201}, response.text
    source = response.json()
    assert source["text"] == expected
    assert source["provenance"]["object_ref"]["backend"] == settings.object_storage_backend
    assert client.get(path + "/" + source["id"] + "/original").content == raw
    columns = {c["name"] for c in inspect(client.app.state.engine).get_columns("story_sources")}
    assert not {"raw_bytes", "text"}.intersection(columns)
    with client.app.state.session_factory() as db:
        row = db.get(StorySource, UUID(source["id"]))
        assert row.sha256 == hashlib.sha256(raw).hexdigest()
        assert row.storage_backend == settings.object_storage_backend.upper()
        assert store.read(row.object_key, 262144, row.sha256) == raw
        assert row.object_key == source_key(UUID(episode["project_id"]), row.id)
        assert row.object_key not in str(source)
        if store.client is not None:
            import httpx

            assert (
                httpx.get(
                    f"{settings.s3_endpoint_url}/{settings.s3_bucket}/{row.object_key}",
                    trust_env=False,
                ).status_code
                == 403
            )


def test_source_retry_is_single_object_and_rollback_removes_only_new_object(
    client, episode, gc_setup, monkeypatch
):
    _, store, _, _ = gc_setup
    payload = source_payload()
    first = client.post(sources_path(episode), json=payload)
    assert first.status_code == 201, first.text
    assert client.post(sources_path(episode), json=payload).json() == first.json()
    captured = []
    original = ObjectStorage.put

    def capture(current, key, data, media_type):
        captured.append(key)
        return original(current, key, data, media_type)

    monkeypatch.setattr(ObjectStorage, "put", capture)

    def refuse(*args):
        raise DomainError("SOURCE_RESPONSE_FAILED", "retry", 409)

    monkeypatch.setattr(source_api, "source_detail", refuse)
    response = client.post(sources_path(episode), json=source_payload(b"Another story"))
    assert response.status_code == 409
    assert len(captured) == 1
    with pytest.raises(DomainError):
        store.read(captured[0], 262144)
    assert client.get(sources_path(episode) + "/" + first.json()["id"] + "/original").content
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(StorySource)) == 1


def test_source_storage_failure_does_not_register_success(client, episode, gc_setup, monkeypatch):
    def fail(*args):
        raise DomainError("STORAGE_UNAVAILABLE", "test failure", 503)

    monkeypatch.setattr(ObjectStorage, "put", fail)
    assert client.post(sources_path(episode), json=source_payload()).status_code == 503
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(StorySource)) == 0


def test_missing_original_blocks_new_work_but_not_a_captured_task(client, episode, gc_setup):
    settings, store, _, _ = gc_setup
    source = client.post(sources_path(episode), json=source_payload()).json()
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/story-analysis-runs",
        json={"source_id": source["id"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    with client.app.state.session_factory() as db:
        captured = db.get(WorkflowRun, run_id).input_json
        key = db.get(StorySource, UUID(source["id"])).object_key
    store.delete(key)
    assert client.get(sources_path(episode)).status_code == 200
    assert client.get(sources_path(episode) + "/" + source["id"]).status_code == 409
    assert client.get(sources_path(episode) + "/" + source["id"] + "/original").status_code == 409
    execute_run(client.app.state.session_factory, settings, run_id, 0)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.status == "SUCCEEDED" and run.input_json == captured
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/story-analysis-runs",
        json={"source_id": source["id"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_OBJECT_UNAVAILABLE"


def test_source_cleanup_retains_history_and_late_references(client, episode, gc_setup):
    settings, store, root, clock = gc_setup
    source = client.post(sources_path(episode), json=source_payload()).json()
    key = source_key(UUID(episode["project_id"]), UUID(source["id"]))
    orphan = source_key(UUID(episode["project_id"]), uuid4())
    store.put(orphan, b"interrupted source save", "text/plain")
    clock[0] += timedelta(days=8)
    planned = plan(client, root, "SOURCE")
    assert planned["retained_count"] == 1 and len(planned["items"]) == 1
    clock[0] += timedelta(hours=2)
    assert run_item(client, root, planned).json()["status"] == "DELETED"
    assert store.read(key, 262144)
    assert client.get(sources_path(episode) + "/" + source["id"] + "/original").status_code == 200


def legacy_source(client, episode, *, corrupt=False):
    import legacy_story_sources as legacy

    decoded = legacy.decode_inline_text("\ufeff Legacy original\r\n", "legacy.txt")
    if corrupt:
        decoded = replace(decoded, sha256="a" * 64)
    with client.app.state.session_factory.begin() as db:
        row = legacy.save_source(
            db,
            db.get(Episode, UUID(episode["id"])),
            "service-owner",
            legacy.SourceDraft("legacy.txt", "text/plain", decoded, "Legacy", "Original"),
            uuid4(),
            "b" * 64,
        )
        return row.id, decoded.raw


def test_migration_copies_originals_and_preserves_ids_and_snapshots(
    client, episode, gc_setup, migrated
):
    _, store, _, _ = gc_setup
    command.downgrade(migrated, "0025")
    source_id, raw = legacy_source(client, episode)
    with client.app.state.session_factory.begin() as db:
        run = WorkflowRun(
            episode_id=UUID(episode["id"]),
            trace_id="legacy",
            idempotency_key="legacy",
            request_fingerprint="a" * 64,
            input_json={
                "story": raw.decode(),
                "source_id": str(source_id),
                "source_revision": {"object_ref": {"backend": "database"}},
            },
        )
        db.add(run)
        db.flush()
        run_id, snapshot = run.id, run.input_json
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert client.get(sources_path(episode) + "/" + str(source_id) + "/original").content == raw
    assert store.read(source_key(UUID(episode["project_id"]), source_id), 262144) == raw
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, run_id).input_json == snapshot
        assert db.get(StorySource, source_id).sha256 == hashlib.sha256(raw).hexdigest()
    with pytest.raises(RuntimeError, match="Cannot downgrade object-backed story sources"):
        command.downgrade(migrated, "0025")


def test_failed_copy_preserves_sql_and_retry_adopts_only_identical_objects(
    client, episode, gc_setup, migrated, monkeypatch
):
    _, store, _, _ = gc_setup
    command.downgrade(migrated, "0025")
    sources = [legacy_source(client, episode), legacy_source(client, episode)]
    original = ObjectStorage.read
    calls = []

    def fail_second(current, key, *args):
        calls.append(key)
        if len(calls) == 2:
            raise DomainError("STORAGE_UNAVAILABLE", "Interrupted copy", 503)
        return original(current, key, *args)

    monkeypatch.setattr(ObjectStorage, "read", fail_second)
    with pytest.raises(RuntimeError, match="original database content retained"):
        command.upgrade(migrated, "head")
    columns = {c["name"] for c in inspect(client.app.state.engine).get_columns("story_sources")}
    assert "raw_bytes" in columns and "storage_backend" not in columns
    with client.app.state.engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0025"
        assert [bytes(r[0]) for r in db.execute(text("SELECT raw_bytes FROM story_sources"))] == [
            raw for _, raw in sources
        ]
    monkeypatch.setattr(ObjectStorage, "read", original)
    command.upgrade(migrated, "head")
    assert all(
        store.read(source_key(UUID(episode["project_id"]), sid), 262144) == raw
        for sid, raw in sources
    )


@pytest.mark.parametrize("mode", ["corrupt", "conflict"])
def test_migration_refuses_corrupt_original_and_conflicting_existing_copy(
    client, episode, gc_setup, migrated, mode
):
    _, store, _, _ = gc_setup
    command.downgrade(migrated, "0025")
    source_id, raw = legacy_source(client, episode, corrupt=mode == "corrupt")
    if mode == "conflict":
        store.put(
            source_key(UUID(episode["project_id"]), source_id), b"different bytes", "text/plain"
        )
    with pytest.raises(RuntimeError, match="original database content retained"):
        command.upgrade(migrated, "head")
    with client.app.state.engine.connect() as db:
        assert bytes(db.scalar(text("SELECT raw_bytes FROM story_sources"))) == raw
    if mode == "conflict":
        assert (
            store.read(source_key(UUID(episode["project_id"]), source_id), 262144)
            == b"different bytes"
        )


def test_schema_failure_restores_originals_and_guards_then_retries(
    client, episode, gc_setup, migrated
):
    command.downgrade(migrated, "0025")
    source_id, raw = legacy_source(client, episode)

    def fail_guard_install(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("CREATE TRIGGER immutable_story_sources_"):
            raise RuntimeError("Interrupted guard installation")
        if statement.startswith("CREATE FUNCTION immutable_story_sources_guard"):
            raise RuntimeError("Interrupted guard installation")

    event.listen(Engine, "before_cursor_execute", fail_guard_install)
    try:
        with pytest.raises(RuntimeError, match="Interrupted guard installation"):
            command.upgrade(migrated, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_guard_install)
    with client.app.state.engine.connect() as db:
        assert bytes(db.scalar(text("SELECT raw_bytes FROM story_sources"))) == raw
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0025"
    with (
        pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"),
        client.app.state.engine.begin() as db,
    ):
        db.execute(text("UPDATE story_sources SET original_name='tampered.txt'"))
    command.upgrade(migrated, "head")
    assert client.get(sources_path(episode) + "/" + str(source_id) + "/original").content == raw


def test_corrupt_object_is_rejected_and_metadata_stays_immutable(client, episode, gc_setup):
    _, store, _, _ = gc_setup
    source = client.post(sources_path(episode), json=source_payload()).json()
    with client.app.state.session_factory() as db:
        row = db.get(StorySource, UUID(source["id"]))
        key, checksum = row.object_key, row.sha256
    store.delete(key)
    store.put(key, b"Corrupt external replacement", "text/plain")
    response = client.get(sources_path(episode) + "/" + source["id"] + "/original")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_OBJECT_UNAVAILABLE"
    with client.app.state.session_factory() as db:
        assert db.get(StorySource, UUID(source["id"])).sha256 == checksum


def test_existing_source_uses_its_recorded_storage_backend(client, episode, gc_setup):
    settings, _, _, _ = gc_setup
    raw = b"Historical original"
    source = client.post(sources_path(episode), json=source_payload(raw)).json()
    recorded = settings.object_storage_backend
    settings.object_storage_backend = "local" if recorded == "s3" else "s3"
    try:
        response = client.get(sources_path(episode) + "/" + source["id"] + "/original")
        assert response.status_code == 200 and response.content == raw
        assert (
            client.get(sources_path(episode) + "/" + source["id"]).json()["provenance"][
                "object_ref"
            ]["backend"]
            == recorded
        )
    finally:
        settings.object_storage_backend = recorded
