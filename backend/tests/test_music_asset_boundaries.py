"""Source integrity, permissions, rollback, and real PostgreSQL music import races."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from queue import Queue
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session
from test_audio_production import wave_bytes
from test_collaboration import register
from test_media_validation import wave_bytes as silent_wave
from test_music_assets import (
    create_music,
    import_music,
    music_payload,
    music_source,
    music_url,
    output_file,
)

from arclight.application.music_assets import music_resource_facts
from arclight.core.errors import DomainError
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    FileRegistration,
    Project,
    ProjectMember,
    StoredFile,
    VisualAsset,
)
from arclight.storage.objects import ObjectStorage


def import_endpoint(asset, file_id):
    return f"/api/v1/projects/{asset['project_id']}/files/{file_id}/asset-versions"


def music_counts(client, asset):
    with client.app.state.session_factory() as db:
        versions = select(AssetVersion.id).where(AssetVersion.asset_id == UUID(asset["id"]))
        return (
            db.scalar(
                select(func.count()).select_from(AssetVersion).where(AssetVersion.id.in_(versions))
            ),
            db.scalar(
                select(func.count())
                .select_from(StoredFile)
                .where(StoredFile.asset_version_id.in_(versions))
            ),
            db.scalar(
                select(func.count())
                .select_from(FileRegistration)
                .where(FileRegistration.asset_version_id.in_(versions))
            ),
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.resource_id.in_(versions), AuditEvent.action == "music.imported")
            ),
        )


@pytest.mark.parametrize("problem", ["uncompleted", "purpose", "rights", "source", "license"])
def test_music_rejects_uncompleted_wrong_purpose_or_unlicensed_sources(client, episode, problem):
    asset = create_music(client, episode)
    changes = {
        "uncompleted": {"completed": False},
        "purpose": {"purpose": "AUDIO"},
        "rights": {"rights_confirmed": False},
        "source": {"source_note": "  "},
        "license": {"rights_note": "  "},
    }[problem]
    source_id = music_source(client, episode, **changes)
    response = client.post(import_endpoint(asset, source_id), json=music_payload(asset))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "MUSIC_SOURCE_INVALID"
    assert music_counts(client, asset) == (0, 0, 0, 0)
    assert client.get(music_url(asset)).json()["revision"] == 0


@pytest.mark.parametrize(
    "data", [b"not a wave file", silent_wave()], ids=["invalid-container", "silent-wave"]
)
def test_music_checks_actual_decoding_and_silence_without_registering(client, episode, data):
    asset = create_music(client, episode)
    source_id = music_source(client, episode, data=data)
    response = client.post(import_endpoint(asset, source_id), json=music_payload(asset))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] in {"INVALID_FILE", "MUSIC_SILENT"}
    assert music_counts(client, asset) == (0, 0, 0, 0)


def test_music_import_idempotency_cas_and_immutable_role(client, episode):
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    payload = music_payload(asset)
    url = import_endpoint(asset, source_id)
    first = client.post(url, json=payload)
    assert first.status_code == 201, first.text
    assert client.post(url, json=payload).json() == first.json()
    changed = client.post(url, json={**payload, "purpose_role": "TRANSITION"})
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    stale = client.post(url, json={**payload, "creation_key": str(uuid4())})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
    other = music_source(client, episode)
    changed_source = client.post(import_endpoint(asset, other), json=payload)
    assert changed_source.status_code == 409
    assert music_counts(client, asset) == (1, 1, 1, 1)


@pytest.mark.parametrize(
    "current_role,expected_status",
    [("viewer", 403), (None, 404)],
    ids=["downgraded-viewer", "membership-revoked"],
)
def test_music_import_replay_rechecks_current_write_membership(
    client, episode, current_role, expected_status
):
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    owner = client.headers["Authorization"]
    member = register(client, "music-import-replay@example.com")
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(asset["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="editor",
        )
        db.add(membership)
        db.flush()
        membership_id = membership.id

    payload = music_payload(asset)
    url = import_endpoint(asset, source_id)
    first = client.post(url, json=payload)
    assert first.status_code == 201, first.text
    original_version = first.json()
    assert client.post(url, json=payload).json() == original_version
    assert music_counts(client, asset) == (1, 1, 1, 1)
    with client.app.state.session_factory.begin() as db:
        membership = db.get(ProjectMember, membership_id)
        if current_role is None:
            db.delete(membership)
        else:
            membership.role = current_role

    denied = client.post(url, json=payload)
    assert denied.status_code == expected_status, denied.text
    assert denied.json()["error"]["code"] == (
        "FORBIDDEN" if current_role == "viewer" else "NOT_FOUND"
    )
    assert music_counts(client, asset) == (1, 1, 1, 1)
    with client.app.state.session_factory() as db:
        assert db.get(VisualAsset, UUID(asset["id"])).revision == 1
        assert db.scalar(select(func.count()).select_from(StoredFile)) == 2

    client.headers["Authorization"] = owner
    assert (
        client.get(music_url(asset, f"/versions/{original_version['id']}")).json()
        == original_version
    )


@pytest.mark.parametrize("role", [None, "background", "VOICE", True])
def test_music_registration_requires_explicit_known_role(client, episode, role):
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    response = client.post(import_endpoint(asset, source_id), json=music_payload(asset, role))
    assert response.status_code == 422, response.text
    assert music_counts(client, asset) == (0, 0, 0, 0)


@pytest.mark.parametrize("target", ["music", "missing"])
def test_legacy_file_registration_without_role_keeps_visual_contract(client, episode, target):
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    payload = music_payload(asset, "BACKGROUND")
    payload.pop("purpose_role")
    if target == "missing":
        payload["asset_id"] = str(uuid4())
    response = client.post(import_endpoint(asset, source_id), json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INVALID_CANDIDATE"
    assert music_counts(client, asset) == (0, 0, 0, 0)


def test_music_cross_project_source_and_target_are_hidden(client, episode):
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    other_project = client.post("/api/v1/projects", json={"name": "另一部作品"}).json()
    other_episode = {"project_id": other_project["id"]}
    other_asset = create_music(client, other_episode)
    assert (
        client.post(
            import_endpoint(other_asset, source_id), json=music_payload(other_asset)
        ).status_code
        == 404
    )
    assert (
        client.post(import_endpoint(asset, source_id), json=music_payload(other_asset)).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/projects/{other_project['id']}/files/{source_id}/content").status_code
        == 404
    )
    assert music_counts(client, asset) == (0, 0, 0, 0)


@pytest.mark.parametrize("role", [None, "viewer", "editor"])
def test_music_reads_and_owner_approval_obey_current_membership(client, episode, role):
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    version = import_music(client, asset, source_id)
    file = output_file(client, version)
    owner = client.headers["Authorization"]
    ready = client.patch(
        music_url(asset, f"/versions/{version['id']}/state"),
        json={"status": "READY_FOR_REVIEW", "expected_revision": 0},
    )
    assert ready.status_code == 200, ready.text
    member = register(client, f"music-{role}@example.com")
    membership_id = None
    if role:
        with client.app.state.session_factory.begin() as db:
            membership = ProjectMember(
                project_id=UUID(asset["project_id"]), user_id=UUID(member["user"]["id"]), role=role
            )
            db.add(membership)
            db.flush()
            membership_id = membership.id
    content_url = f"/api/v1/projects/{asset['project_id']}/files/{file.id}/content"
    assert client.get(content_url).status_code == (200 if role else 404)
    approved = client.patch(
        music_url(asset, f"/versions/{version['id']}/state"),
        json={"status": "APPROVED", "expected_revision": 1},
    )
    assert approved.status_code == (403 if role else 404), approved.text
    if role:
        with client.app.state.session_factory.begin() as db:
            db.delete(db.get(ProjectMember, membership_id))
        assert client.get(content_url).status_code == 404
    client.headers["Authorization"] = owner
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(asset["project_id"]))
        project.deleted_at = datetime.now(UTC)
        project.restore_until = datetime.now(UTC)
        project.deletion_id = uuid4()
    assert client.get(content_url).status_code == 404
    assert client.get(music_url(asset, f"/versions/{version['id']}")).status_code == 404


def test_music_failure_after_object_write_rolls_back_and_retries_same_key(
    client, episode, monkeypatch
):
    import arclight.application.music_assets as music

    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    payload = music_payload(asset)
    settings = client.app.state.settings
    before = {path for path in settings.asset_storage_path.rglob("*") if path.is_file()}

    def fail(*_args, **_kwargs):
        raise DomainError("INJECTED_MUSIC_FAILURE", "Interrupted before file registration", 409)

    with monkeypatch.context() as patched:
        patched.setattr(music, "record_asset_file", fail)
        response = client.post(import_endpoint(asset, source_id), json=payload)
        assert response.status_code == 409, response.text
    assert music_counts(client, asset) == (0, 0, 0, 0)
    assert client.get(music_url(asset)).json()["revision"] == 0
    assert {path for path in settings.asset_storage_path.rglob("*") if path.is_file()} == before
    assert (
        client.get(f"/api/v1/projects/{asset['project_id']}/files/{source_id}/content").content
        == wave_bytes()
    )
    retry = client.post(import_endpoint(asset, source_id), json=payload)
    assert retry.status_code == 201, retry.text
    assert music_counts(client, asset) == (1, 1, 1, 1)


@pytest.mark.parametrize("part", ["source", "registration", "output", "report"])
def test_music_positive_review_rechecks_frozen_source_chain_and_actual_report(
    client, episode, part
):
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    other_id = music_source(client, episode)
    version_ids = set()

    def inject_before_first_insert(db, *_args):
        # Simulate an invalid initial producer receipt without updating immutable history.
        for row in list(db.new):
            if isinstance(row, AssetVersion) and row.asset_id == UUID(asset["id"]):
                version_ids.add(row.id)
                metadata = deepcopy(row.metadata_json)
                if part == "source":
                    metadata["audio_resource"]["source_file_id"] = other_id
                    row.metadata_json = metadata
                elif part == "report":
                    metadata["audio_resource"]["validation"]["duration_ms"] += 1
                    row.metadata_json = metadata
            elif (
                part == "registration"
                and isinstance(row, FileRegistration)
                and row.asset_version_id in version_ids
            ):
                row.file_id = UUID(other_id)
            elif (
                part == "output"
                and isinstance(row, StoredFile)
                and row.asset_version_id in version_ids
            ):
                row.rights_note = "A different initial declaration"

    event.listen(Session, "before_flush", inject_before_first_insert)
    try:
        version = import_music(client, asset, source_id)
    finally:
        event.remove(Session, "before_flush", inject_before_first_insert)
    assert version_ids == {UUID(version["id"])}
    response = client.patch(
        music_url(asset, f"/versions/{version['id']}/state"),
        json={"status": "READY_FOR_REVIEW", "expected_revision": 0},
    )
    assert response.status_code == (422 if part == "report" else 409), response.text
    assert client.get(music_url(asset, f"/versions/{version['id']}")).json() == version
    assert version["status"] == "DRAFT" and version["revision"] == 0


def test_music_facts_have_no_object_io_but_positive_review_rechecks_actual_bytes(
    client, episode, monkeypatch
):
    asset = create_music(client, episode)
    version = import_music(client, asset, music_source(client, episode))
    file = output_file(client, version)

    def refuse(*_args, **_kwargs):
        raise AssertionError("facts must never read media")

    with monkeypatch.context() as patched:
        patched.setattr(ObjectStorage, "read", refuse)
        with client.app.state.session_factory() as db:
            row = db.get(AssetVersion, UUID(version["id"]))
            music = db.get(VisualAsset, UUID(asset["id"]))
            assert (
                music_resource_facts(db, music.project_id, music, row).purpose_role == "BACKGROUND"
            )
    store = ObjectStorage(client.app.state.settings)
    # Simulate external corruption of this isolated test object, preserving put-once guards.
    store.delete(file.object_key)
    store.put(file.object_key, b"x" * file.byte_size, file.media_type)
    response = client.patch(
        music_url(asset, f"/versions/{version['id']}/state"),
        json={"status": "READY_FOR_REVIEW", "expected_revision": 0},
    )
    assert response.status_code == 409, response.text
    assert client.get(music_url(asset, f"/versions/{version['id']}")).json() == version
    assert (
        client.get(f"/api/v1/projects/{asset['project_id']}/files/{file.id}/content").status_code
        == 409
    )


def overlapping_music_imports(client, asset, source_id, payloads, monkeypatch):
    import arclight.api.upload_completion as uploads

    entering = Queue()
    original_gate = uploads.require_project

    def observe_request(db, project_id, actor, **options):
        if project_id == UUID(asset["project_id"]) and options.get("write"):
            entering.put(db.scalar(text("SELECT pg_backend_pid()")))
        return original_gate(db, project_id, actor, **options)

    with monkeypatch.context() as patched, ThreadPoolExecutor(max_workers=2) as pool:
        patched.setattr(uploads, "require_project", observe_request)
        with client.app.state.session_factory.begin() as holder:
            holder.scalar(
                select(Project).where(Project.id == UUID(asset["project_id"])).with_for_update()
            )
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            futures = [
                pool.submit(client.post, import_endpoint(asset, source_id), json=payload)
                for payload in payloads
            ]
            request_pids = {entering.get(timeout=10), entering.get(timeout=10)}
            assert len(request_pids) == 2
            deadline = monotonic() + 10
            blockers = {}
            while monotonic() < deadline:
                blockers = {
                    pid: holder.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": pid})
                    for pid in request_pids
                }
                if all(blockers.values()):
                    break
                sleep(0.01)
            assert all(blockers.values()), "Both requests must demonstrably wait on a real row lock"
            participants = request_pids | {holder_pid}
            assert all(set(values) <= participants for values in blockers.values())
        # Releasing the holder precedes joining workers, including on assertion failure.
        responses = [future.result(timeout=30) for future in futures]
    return responses, {"holder": holder_pid, "request_blockers": blockers}


def test_music_postgres_concurrent_identical_imports_create_one_version_and_replay(
    client, episode, monkeypatch, record_property
):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    payload = music_payload(asset)
    responses, overlap = overlapping_music_imports(
        client, asset, source_id, [payload, payload], monkeypatch
    )
    record_property("music_real_lock_overlap", repr(overlap))
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert (
        client.post(import_endpoint(asset, source_id), json=payload).json() == responses[0].json()
    )
    assert music_counts(client, asset) == (1, 1, 1, 1)


def test_music_postgres_concurrent_new_keys_share_cas_and_keep_only_one_output(
    client, episode, monkeypatch, record_property
):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    payloads = [music_payload(asset), music_payload(asset)]
    responses, overlap = overlapping_music_imports(client, asset, source_id, payloads, monkeypatch)
    record_property("music_real_lock_overlap", repr(overlap))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert music_counts(client, asset) == (1, 1, 1, 1)
