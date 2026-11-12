"""External audio production consumes actual private, completed-upload bytes."""

import hashlib
import io
import json
import math
import struct
import wave
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import av
import pytest
from sqlalchemy import func, select
from test_audio_production import audio_source, output_audio, snapshot, wave_bytes
from test_collaboration import register
from test_media_validation import wave_bytes as silent_wave
from test_text_contract_v2 import legacy_script, save_script

from arclight.application.audio_production import input_context
from arclight.application.dependencies import capture_manifest
from arclight.core.errors import DomainError
from arclight.domain.dependencies import DependencyInput, ManifestCreate
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    DependencyOutput,
    Episode,
    FileRegistration,
    Project,
    ProjectMember,
    StoredFile,
    UploadCompletion,
    UploadIntent,
    VisualAsset,
)
from arclight.storage.objects import ObjectStorage

__all__ = ["audio_source"]


def import_target(client, source, **changes):
    with client.app.state.session_factory.begin() as db:
        row = VisualAsset(
            **{
                "project_id": UUID(source[0]["project_id"]),
                "asset_type": "AUDIO",
                "name": "外部配音",
                **changes,
            }
        )
        db.add(row)
        db.flush()
        return str(row.id)


def completed_source(client, source, *, data=None, completed=True, purpose="AUDIO", **changes):
    """A real local object with immutable upload history; public S3 flow is separate."""
    data = wave_bytes() if data is None else data
    settings = client.app.state.settings
    project_id = UUID(source[0]["project_id"])
    identity = uuid4()
    key = f"validated/{project_id}/{identity}"
    media_type = changes.pop("media_type", "audio/wav")
    ObjectStorage(settings).put(key, data, media_type)
    declaration = {
        "source_note": "Original recording for this production",
        "rights_note": "Original test waveform; permission granted",
        "rights_confirmed": True,
    }
    declaration.update({k: v for k, v in changes.items() if k in declaration})
    with client.app.state.session_factory.begin() as db:
        file = StoredFile(
            id=identity,
            project_id=project_id,
            asset_version_id=None,
            storage_backend=settings.object_storage_backend.upper(),
            bucket=settings.s3_bucket if settings.object_storage_backend == "s3" else None,
            object_key=key,
            checksum=hashlib.sha256(data).hexdigest(),
            byte_size=len(data),
            media_type=media_type,
            original_name={
                "audio/wav": "original.wav",
                "audio/mpeg": "original.mp3",
                "audio/ogg": "original.ogg",
            }[media_type],
            created_by="service-owner",
            **declaration,
        )
        db.add(file)
        db.flush()
        if completed:
            upload = UploadIntent(
                project_id=project_id,
                bucket="arclight-test",
                object_key=f"temporary/{project_id}/{uuid4()}",
                purpose=purpose,
                original_name=file.original_name,
                declared_media_type=media_type,
                byte_size=len(data),
                checksum=file.checksum,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                created_by="service-owner",
            )
            db.add(upload)
            db.flush()
            db.add(
                UploadCompletion(
                    upload_id=upload.id,
                    file_id=file.id,
                    purpose=purpose,
                    request_sha256=hashlib.sha256(
                        json.dumps(declaration, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                )
            )
    return str(identity)


def import_request(source, asset, file, *, index=0, **changes):
    url = f"/api/v1/audio-snapshots/{source[2]['id']}/lines/{source[3][index]['line_id']}/imports"
    return url, {
        "creation_key": str(uuid4()),
        "target_asset_id": asset,
        "expected_revision": 0,
        "source_file_id": file,
        **changes,
    }


def import_counts(client, asset):
    with client.app.state.session_factory() as db:
        return (
            db.scalar(
                select(func.count())
                .select_from(AssetVersion)
                .where(AssetVersion.asset_id == UUID(asset))
            ),
            db.scalar(
                select(func.count())
                .select_from(DependencyManifest)
                .where(DependencyManifest.target_asset_id == UUID(asset))
            ),
            db.scalar(select(func.count()).select_from(AudioLineBinding)),
            db.scalar(select(func.count()).select_from(FileRegistration)),
        )


def test_import_keeps_original_bytes_provenance_and_exact_unapproved_binding(client, audio_source):
    asset = import_target(client, audio_source)
    source_id = completed_source(client, audio_source)
    url, payload = import_request(audio_source, asset, source_id)
    response = client.post(url, json=payload)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["source_file_id"] == source_id
    assert result["snapshot_id"] == audio_source[2]["id"]
    assert result["line_id"] == audio_source[3][0]["line_id"]
    assert result["file_id"] != source_id
    assert result["validation"]["media_type"] == "audio/wav"
    assert result["validation"]["sample_rate"] == 24000
    assert result["validation"]["channels"] == 1
    assert result["validation"]["duration_ms"] == 100
    settings = client.app.state.settings
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(result["asset_version_id"]))
        original = db.get(StoredFile, UUID(source_id))
        output = db.get(StoredFile, UUID(result["file_id"]))
        binding = db.get(AudioLineBinding, UUID(result["binding_id"]))
        target = db.get(VisualAsset, UUID(asset))
        assert original.asset_version_id is None
        assert output.asset_version_id == version.id
        assert output.source_note == original.source_note
        assert output.rights_note == original.rights_note
        assert output.rights_confirmed
        assert output.checksum == original.checksum == version.checksum
        assert output.object_key != original.object_key
        assert (
            ObjectStorage(settings).read(output.object_key, output.byte_size, output.checksum)
            == wave_bytes()
        )
        assert (
            ObjectStorage(settings).read(original.object_key, original.byte_size, original.checksum)
            == wave_bytes()
        )
        assert version.status == "DRAFT" and not version.stale
        assert target.current_version_id is None
        assert target.revision == 1
        assert binding.asset_version_id == version.id and binding.file_id == output.id
        assert str(binding.dependency_manifest_id) == result["dependency_manifest_id"]
        registration = db.scalar(
            select(FileRegistration).where(FileRegistration.asset_version_id == version.id)
        )
        assert registration.file_id == original.id
        receipt = db.scalar(
            select(DependencyOutput).where(DependencyOutput.asset_version_id == version.id)
        )
        assert receipt.dependency_manifest_id == binding.dependency_manifest_id
        manifest = db.get(DependencyManifest, binding.dependency_manifest_id)
        assert manifest.snapshot_json["inputs"][0]["selectors"] == [f"dialogue:{binding.line_id}"]
        metadata = version.metadata_json["audio_import"]
        assert metadata["schema_version"] == "audio-import/1"
        assert metadata["source_file_id"] == source_id


def test_import_replay_ignores_stale_cas_but_conflicts_on_changed_source_or_line(
    client, audio_source
):
    asset = import_target(client, audio_source)
    file = completed_source(client, audio_source)
    url, payload = import_request(audio_source, asset, file)
    first = client.post(url, json=payload)
    assert first.status_code == 201, first.text
    repeated = client.post(url, json={**payload, "expected_revision": 999})
    assert repeated.status_code == 200 and repeated.json() == first.json()
    another = completed_source(client, audio_source)
    assert client.post(url, json={**payload, "source_file_id": another}).status_code == 409
    other_url, _ = import_request(audio_source, asset, file, index=1)
    assert client.post(other_url, json=payload).status_code == 409
    assert import_counts(client, asset) == (1, 1, 1, 1)


@pytest.mark.parametrize("problem", ["uncompleted", "purpose", "rights", "source", "license"])
def test_import_rejects_unvalidated_or_unlicensed_sources_without_outputs(
    client, audio_source, problem
):
    asset = import_target(client, audio_source)
    options = {
        "uncompleted": {"completed": False},
        "purpose": {"purpose": "MUSIC"},
        "rights": {"rights_confirmed": False},
        "source": {"source_note": "  "},
        "license": {"rights_note": "  "},
    }[problem]
    file = completed_source(client, audio_source, **options)
    url, payload = import_request(audio_source, asset, file)
    response = client.post(url, json=payload)
    assert response.status_code in {409, 422}, response.text
    assert response.json()["error"]["code"] == "AUDIO_SOURCE_INVALID"
    assert import_counts(client, asset) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "problem", ["corrupt", "mislabeled", "rate", "duration", "silent", "damaged_object"]
)
def test_import_checks_actual_media_and_rolls_back_failures(client, audio_source, problem):
    asset = import_target(client, audio_source)
    data = {
        "corrupt": lambda: wave_bytes()[:-10],
        "mislabeled": wave_bytes,
        "rate": lambda: silent_wave(rate=96000),
        "duration": lambda: silent_wave(seconds=601),
        "silent": silent_wave,
        "damaged_object": wave_bytes,
    }[problem]()
    file = completed_source(
        client,
        audio_source,
        data=data,
        **({"media_type": "audio/mpeg"} if problem == "mislabeled" else {}),
    )
    if problem == "damaged_object":
        with client.app.state.session_factory() as db:
            row = db.get(StoredFile, UUID(file))
            key = row.object_key
        storage = ObjectStorage(client.app.state.settings)
        storage.delete(key)
        storage.put(key, b"damaged", "audio/wav")
    url, payload = import_request(audio_source, asset, file)
    response = client.post(url, json=payload)
    assert response.status_code in {409, 422}, response.text
    if problem == "silent":
        assert response.json()["error"]["code"] == "AUDIO_SILENT"
    assert import_counts(client, asset) == (0, 0, 0, 0)
    with client.app.state.session_factory() as db:
        assert db.get(StoredFile, UUID(file)).asset_version_id is None


@pytest.mark.parametrize("state", ["revision", "archived", "locked", "wrong_type", "wrong_project"])
def test_import_obeys_existing_target_asset_contract(client, audio_source, state):
    changes = {"status": "ARCHIVED"} if state == "archived" else {}
    if state == "locked":
        changes["status"] = "LOCKED"
    if state == "wrong_type":
        changes["asset_type"] = "IMAGE"
    if state == "wrong_project":
        project = client.post("/api/v1/projects", json={"name": "Other"}).json()
        changes["project_id"] = UUID(project["id"])
    asset = import_target(client, audio_source, **changes)
    file = completed_source(client, audio_source)
    url, payload = import_request(
        audio_source, asset, file, expected_revision=1 if state == "revision" else 0
    )
    result = client.post(url, json=payload)
    assert result.status_code in {404, 409, 422}, result.text
    assert import_counts(client, asset) == (0, 0, 0, 0)


def test_import_cross_project_source_is_hidden_and_does_not_register(client, audio_source):
    asset = import_target(client, audio_source)
    project = client.post("/api/v1/projects", json={"name": "Other source"}).json()
    other = ({"project_id": project["id"]},)
    file = completed_source(client, other)
    url, payload = import_request(audio_source, asset, file)
    response = client.post(url, json=payload)
    assert response.status_code == 404, response.text
    assert import_counts(client, asset) == (0, 0, 0, 0)


def test_import_revalidates_membership_before_replay_and_respects_archive(client, audio_source):
    asset = import_target(client, audio_source)
    file = completed_source(client, audio_source)
    url, payload = import_request(audio_source, asset, file)
    owner_header = client.headers["Authorization"]
    member = register(client, "audio-import-member@example.com")
    assert client.post(url, json=payload).status_code == 404
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(audio_source[0]["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="viewer",
        )
        db.add(membership)
        db.flush()
        member_id = membership.id
    assert client.post(url, json=payload).status_code == 403
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, member_id).role = "editor"
    first = client.post(url, json=payload)
    assert first.status_code == 201, first.text
    history_url = url + "/" + first.json()["asset_version_id"]
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, member_id).role = "viewer"
    assert client.get(history_url).json() == first.json()
    assert client.post(url, json=payload).status_code == 403
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, member_id))
    assert client.post(url, json=payload).status_code == 404
    assert client.get(history_url).status_code == 404
    client.headers["Authorization"] = owner_header
    for model, identity, attribute, value, original in [
        (Episode, audio_source[0]["id"], "status", "ARCHIVED", "DRAFT"),
        (Project, audio_source[0]["project_id"], "archived", True, False),
    ]:
        with client.app.state.session_factory.begin() as db:
            setattr(db.get(model, UUID(identity)), attribute, value)
        assert client.post(url, json=payload).status_code == 409
        assert client.get(history_url).json() == first.json()
        with client.app.state.session_factory.begin() as db:
            setattr(db.get(model, UUID(identity)), attribute, original)
    assert import_counts(client, asset) == (1, 1, 1, 1)


def test_imported_audio_keeps_original_history_and_exact_line_invalidation(client, audio_source):
    assets = [import_target(client, audio_source) for _ in range(2)]
    file = completed_source(client, audio_source)
    imported = []
    requests = []
    for index, asset in enumerate(assets):
        url, payload = import_request(audio_source, asset, file, index=index)
        response = client.post(url, json=payload)
        assert response.status_code == 201, response.text
        imported.append(response.json())
        requests.append((url, payload))
    episode, script, snapshot, lines = audio_source
    content = deepcopy(script["content_json"])
    content["scenes"][0]["dialogue"][0]["text"] = "Updated imported line."
    prefix = f"/api/v1/episodes/{episode['id']}/scripts"
    saved = client.post(
        prefix, json={"expected_version_no": 1, "base_version_id": script["id"], "content": content}
    )
    assert saved.status_code == 201, saved.text
    assert (
        client.post(
            prefix + f"/{saved.json()['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, UUID(imported[0]["asset_version_id"])).stale
        assert not db.get(AssetVersion, UUID(imported[1]["asset_version_id"])).stale
        assert db.get(StoredFile, UUID(file)).asset_version_id is None
    assert client.get(f"/api/v1/audio-snapshots/{snapshot['id']}/lines").json() == lines
    late_url, late_payload = import_request(audio_source, assets[0], file, expected_revision=1)
    assert client.post(late_url, json=late_payload).status_code == 409
    input_url = late_url.removesuffix("/imports") + "/inputs"
    rejected_input = client.post(
        input_url,
        json={"creation_key": str(uuid4()), "target_asset_id": assets[0]},
    )
    assert rejected_input.status_code == 409, rejected_input.text
    assert rejected_input.json()["error"]["code"] == "AUDIO_LINE_NOT_USABLE"
    original_input = client.post(
        input_url,
        json={
            "creation_key": requests[0][1]["creation_key"],
            "target_asset_id": assets[0],
        },
    )
    assert original_input.status_code == 201, original_input.text
    assert original_input.json()["dependency_manifest_id"] == imported[0]["dependency_manifest_id"]
    assert import_counts(client, assets[0]) == (1, 1, 2, 2)
    unaffected_url, unaffected_payload = import_request(
        audio_source, assets[1], file, index=1, expected_revision=1
    )
    unaffected = client.post(unaffected_url, json=unaffected_payload)
    assert unaffected.status_code == 201, unaffected.text
    assert unaffected.json()["asset_version_id"] != imported[1]["asset_version_id"]
    assert import_counts(client, assets[1]) == (2, 2, 3, 3)
    bypass_asset = import_target(client, audio_source)
    with client.app.state.session_factory.begin() as db:
        frozen = db.get(AudioSnapshot, UUID(snapshot["id"]))
        manifest, _ = capture_manifest(
            db,
            UUID(episode["project_id"]),
            "service-owner",
            ManifestCreate(
                creation_key=uuid4(),
                target_asset_id=UUID(bypass_asset),
                inputs=[
                    DependencyInput(
                        source_asset_version_id=UUID(script["id"]),
                        dependency_type="CONTENT_DEPENDENCY",
                        selectors=[f"dialogue:{lines[0]['line_id']}"],
                    )
                ],
                context=input_context(frozen, lines[0]["line_id"]),
            ),
        )
        manifest_id = str(manifest.id)
    bypass_version = output_audio(client, audio_source, bypass_asset, manifest_id)
    with client.app.state.session_factory() as db:
        assert not db.get(AssetVersion, UUID(bypass_version)).stale
    binding_url = requests[0][0].removesuffix("/imports") + "/bindings"
    rejected_binding = client.post(binding_url, json={"asset_version_id": bypass_version})
    assert rejected_binding.status_code == 409, rejected_binding.text
    assert import_counts(client, bypass_asset) == (1, 1, 3, 3)
    for (old_url, _), original_import in zip(requests, imported, strict=True):
        assert (
            client.get(old_url + "/" + original_import["asset_version_id"]).json()
            == original_import
        )


def test_postgres_import_concurrent_replay_creates_exactly_one_version(client, audio_source):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    asset = import_target(client, audio_source)
    file = completed_source(client, audio_source)
    url, payload = import_request(audio_source, asset, file)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json=payload), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert responses[0].json() == responses[1].json()
    assert import_counts(client, asset) == (1, 1, 1, 1)


def test_import_allocates_a_stable_target_and_requires_cas_for_a_new_version(client, audio_source):
    file = completed_source(client, audio_source)
    url, payload = import_request(audio_source, None, file)
    del payload["target_asset_id"]
    first = client.post(url, json=payload)
    assert first.status_code == 201, first.text
    result = first.json()
    assert result["asset_revision"] == 1
    replacement = {**payload, "creation_key": str(uuid4())}
    assert client.post(url, json=replacement).status_code == 409
    second = client.post(url, json={**replacement, "expected_revision": result["asset_revision"]})
    assert second.status_code == 201, second.text
    assert second.json()["target_asset_id"] == result["target_asset_id"]
    assert second.json()["asset_version_id"] != result["asset_version_id"]
    assert second.json()["asset_revision"] == 2
    assert client.get(url + "/" + result["asset_version_id"]).json() == result
    assert import_counts(client, result["target_asset_id"]) == (2, 2, 2, 2)


def test_import_failure_after_object_write_keeps_candidate_and_allows_retry(
    client, audio_source, monkeypatch
):
    import arclight.application.audio_import as imports

    asset = import_target(client, audio_source)
    file = completed_source(client, audio_source)
    url, payload = import_request(audio_source, asset, file)
    original_objects = set(client.app.state.settings.asset_storage_path.rglob("*"))

    def fail_binding(*_args, **_kwargs):
        raise DomainError("INJECTED_IMPORT_FAILURE", "Interrupted before line binding", 409)

    with monkeypatch.context() as patched:
        patched.setattr(imports, "bind_audio", fail_binding)
        response = client.post(url, json=payload)
        assert response.status_code == 409, response.text
    assert import_counts(client, asset) == (0, 0, 0, 0)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(StoredFile)) == 1
        source = db.get(StoredFile, UUID(file))
        assert (
            ObjectStorage(client.app.state.settings).read(
                source.object_key, source.byte_size, source.checksum
            )
            == wave_bytes()
        )
    assert {
        path for path in client.app.state.settings.asset_storage_path.rglob("*") if path.is_file()
    } == {path for path in original_objects if path.is_file()}
    retried = client.post(url, json=payload)
    assert retried.status_code == 201, retried.text
    assert import_counts(client, asset) == (1, 1, 1, 1)


def compressed_voice(media_type):
    output = io.BytesIO()
    codec, format_name, samples = (
        ("libmp3lame", "mp3", 1152) if media_type == "audio/mpeg" else ("libopus", "ogg", 960)
    )
    with av.open(output, "w", format=format_name) as container:
        stream = container.add_stream(codec, rate=48000)
        stream.layout = "mono"
        for index in range(4):
            frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
            raw = b"".join(
                struct.pack("<h", int(4000 * math.sin((index * samples + sample) / 20)))
                for sample in range(samples)
            )
            frame.planes[0].update(raw + bytes(frame.planes[0].buffer_size - len(raw)))
            frame.sample_rate = 48000
            frame.pts = index * samples
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()


@pytest.mark.parametrize("media_type", ["audio/mpeg", "audio/ogg"])
def test_import_accepts_fully_decoded_compressed_voice_and_persists_report(
    client, audio_source, media_type
):
    asset = import_target(client, audio_source)
    data = compressed_voice(media_type)
    file = completed_source(client, audio_source, data=data, media_type=media_type)
    url, payload = import_request(audio_source, asset, file)
    response = client.post(url, json=payload)
    assert response.status_code == 201, response.text
    result = response.json()
    validation = result["validation"]
    assert validation["media_type"] == media_type
    assert validation["sample_rate"] == 48000
    assert validation["duration_ms"] > 0 and validation["channels"] == 1
    assert validation["codec"] in {"mp3", "mp3float", "opus", "vorbis"}
    assert not validation["silence"]["silent"]
    assert 0 < validation["silence"]["peak"] <= 1
    assert 0 < validation["silence"]["rms"] <= 1
    assert client.get(url + "/" + result["asset_version_id"]).json() == result
    with client.app.state.session_factory() as db:
        receipt = db.get(StoredFile, UUID(result["file_id"]))
        assert (
            ObjectStorage(client.app.state.settings).read(
                receipt.object_key, len(data), receipt.checksum
            )
            == data
        )


def test_import_rejects_real_audio_shorter_than_one_millisecond(client, audio_source):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b"\xff\x1f")
    asset = import_target(client, audio_source)
    file = completed_source(client, audio_source, data=output.getvalue())
    url, payload = import_request(audio_source, asset, file)
    response = client.post(url, json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_FILE"
    assert import_counts(client, asset) == (0, 0, 0, 0)


def test_import_report_is_scoped_to_the_exact_snapshot_and_line(client, audio_source):
    asset = import_target(client, audio_source)
    file = completed_source(client, audio_source)
    url, payload = import_request(audio_source, asset, file)
    response = client.post(url, json=payload)
    assert response.status_code == 201, response.text
    version_id = response.json()["asset_version_id"]
    other_line_url, _ = import_request(audio_source, asset, file, index=1)
    assert client.get(other_line_url + "/" + version_id).status_code == 404
    other_snapshot = client.post(
        f"/api/v1/episodes/{audio_source[0]['id']}/audio-snapshots",
        json={"creation_key": str(uuid4()), "script_version_id": audio_source[1]["id"]},
    )
    assert other_snapshot.status_code == 201, other_snapshot.text
    wrong_snapshot = url.replace(audio_source[2]["id"], other_snapshot.json()["id"])
    assert client.get(wrong_snapshot + "/" + version_id).status_code == 404
    assert client.get(url + "/" + version_id).json() == response.json()


def test_failed_import_does_not_leave_an_automatically_allocated_target(
    client, audio_source, monkeypatch
):
    import arclight.application.audio_import as imports

    file = completed_source(client, audio_source)
    url, payload = import_request(audio_source, None, file)
    del payload["target_asset_id"]
    with client.app.state.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(VisualAsset))

    def fail_binding(*_args, **_kwargs):
        raise DomainError("INJECTED_IMPORT_FAILURE", "Interrupted before line binding", 409)

    with monkeypatch.context() as patched:
        patched.setattr(imports, "bind_audio", fail_binding)
        assert client.post(url, json=payload).status_code == 409
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(VisualAsset)) == before
        assert db.scalar(select(func.count()).select_from(AudioLineBinding)) == 0
        assert db.scalar(select(func.count()).select_from(FileRegistration)) == 0
        assert db.scalar(select(func.count()).select_from(StoredFile)) == 1
    response = client.post(url, json=payload)
    assert response.status_code == 201, response.text
    assert import_counts(client, response.json()["target_asset_id"]) == (1, 1, 1, 1)


def test_deleted_project_hides_import_history_and_replay_without_deleting_files(
    client, audio_source
):
    asset = import_target(client, audio_source)
    file = completed_source(client, audio_source)
    url, payload = import_request(audio_source, asset, file)
    response = client.post(url, json=payload)
    assert response.status_code == 201, response.text
    result = response.json()
    project_url = f"/api/v1/projects/{audio_source[0]['project_id']}"
    project = client.get(project_url).json()
    archived = client.patch(project_url, json={"archived": True})
    assert archived.status_code == 200, archived.text
    deleted = client.request("DELETE", project_url, json={"confirm_name": project["name"]})
    assert deleted.status_code == 200, deleted.text
    assert client.get(url + "/" + result["asset_version_id"]).status_code == 404
    assert client.post(url, json=payload).status_code == 404
    assert import_counts(client, asset) == (1, 1, 1, 1)
    with client.app.state.session_factory() as db:
        for file_id in (file, result["file_id"]):
            row = db.get(StoredFile, UUID(file_id))
            assert (
                ObjectStorage(client.app.state.settings).read(
                    row.object_key, row.byte_size, row.checksum
                )
                == wave_bytes()
            )


def test_snapshot_line_reuse_obeys_multiple_approved_revisions(client, episode):
    content = legacy_script()
    content["scenes"][0]["dialogue"].append({"speaker": "主角", "text": "第三句始终保持不变。"})
    script = save_script(client, episode, content=content)
    captured = snapshot(client, episode, script)
    assert captured.status_code == 201, captured.text
    frozen = captured.json()
    lines = client.get(f"/api/v1/audio-snapshots/{frozen['id']}/lines").json()
    source = (episode, script, frozen, lines)
    file = completed_source(client, source)
    first_asset = import_target(client, source)
    third_asset = import_target(client, source)
    first_url, first_payload = import_request(source, first_asset, file)
    first = client.post(first_url, json=first_payload)
    assert first.status_code == 201, first.text
    third_url, third_payload = import_request(source, third_asset, file, index=2)
    third = client.post(third_url, json=third_payload)
    assert third.status_code == 201, third.text
    prefix = f"/api/v1/episodes/{episode['id']}/scripts"
    changed_b = deepcopy(script["content_json"])
    changed_b["scenes"][0]["dialogue"][1]["text"] = "Only line B changes in revision two."
    second_script = client.post(
        prefix,
        json={"expected_version_no": 1, "base_version_id": script["id"], "content": changed_b},
    )
    assert second_script.status_code == 201, second_script.text
    second_row = second_script.json()
    assert (
        client.post(
            prefix + f"/{second_row['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    reused = client.post(
        first_url, json={**first_payload, "creation_key": str(uuid4()), "expected_revision": 1}
    )
    assert reused.status_code == 201, reused.text
    with client.app.state.session_factory() as db:
        assert not db.get(AssetVersion, UUID(first.json()["asset_version_id"])).stale
        assert not db.get(AssetVersion, UUID(reused.json()["asset_version_id"])).stale
    changed_a = deepcopy(second_row["content_json"])
    changed_a["scenes"][0]["dialogue"][0]["text"] = "Line A changes in revision three."
    third_script = client.post(
        prefix,
        json={"expected_version_no": 2, "base_version_id": second_row["id"], "content": changed_a},
    )
    assert third_script.status_code == 201, third_script.text
    third_script_url = prefix + f"/{third_script.json()['id']}"
    preview = client.get(third_script_url + "/dependency-impact")
    assert preview.status_code == 200, preview.text
    impact = preview.json()
    affected = {item["asset_version_id"] for item in impact["affected"]}
    assert {first.json()["asset_version_id"], reused.json()["asset_version_id"]} <= affected
    assert third.json()["asset_version_id"] not in affected
    approved = client.post(
        third_script_url + "/review",
        json={"decision": "APPROVE", "impact_digest": impact["digest"]},
    )
    assert approved.status_code == 200, approved.text
    with client.app.state.session_factory() as db:
        previous_stale = {
            "A_v1": db.get(AssetVersion, UUID(first.json()["asset_version_id"])).stale,
            "A_reused_after_B": db.get(AssetVersion, UUID(reused.json()["asset_version_id"])).stale,
            "C_v1": db.get(AssetVersion, UUID(third.json()["asset_version_id"])).stale,
        }
        assert previous_stale == {"A_v1": True, "A_reused_after_B": True, "C_v1": False}
    assert client.get(first_url + "/" + first.json()["asset_version_id"]).json() == first.json()
    assert client.get(first_url + "/" + reused.json()["asset_version_id"]).json() == reused.json()
    third_reused = client.post(
        third_url, json={**third_payload, "creation_key": str(uuid4()), "expected_revision": 1}
    )
    assert third_reused.status_code == 201, third_reused.text
    rejected = client.post(
        first_url, json={**first_payload, "creation_key": str(uuid4()), "expected_revision": 2}
    )
    assert rejected.status_code == 409, {
        "response": rejected.json(),
        "previous_stale": previous_stale,
    }
    rejected_input = client.post(
        first_url.removesuffix("/imports") + "/inputs",
        json={"creation_key": str(uuid4()), "target_asset_id": first_asset},
    )
    assert rejected_input.status_code == 409, rejected_input.text
