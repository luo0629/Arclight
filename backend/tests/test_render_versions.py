"""RENDER-10 appends real inspected renders to the existing version and DAG registries."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from uuid import UUID, uuid4, uuid5

import av
import pytest
from pydantic import ValidationError
from render_admission_helpers import edited, ready_manifest
from render_job_helpers import job_case, read, render_case, review_case, run, submit, timeline_case
from sqlalchemy import func, select
from test_dependency_invalidation import apply
from test_dependency_validation import frozen_manifest, registry_version
from test_timeline_saves import duration_edit

from arclight.application import render_execution
from arclight.application.dependencies import attach_output, capture_manifest, input_usable
from arclight.application.render_versions import RenderInputs
from arclight.domain.dependencies import DependencyInput, ManifestCreate
from arclight.domain.timelines import TimelineSnapshot
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    AuditEvent,
    DependencyManifest,
    DependencyOutput,
    RenderArtifact,
    RenderAttempt,
    RenderExecution,
    RenderInspection,
    StoredFile,
    Timeline,
    TimelineClip,
    TimelineClipVisual,
    TimelineTrack,
    VisualAsset,
)
from arclight.storage.cleanup import snapshot
from arclight.storage.objects import ObjectStorage

__all__ = ["job_case", "render_case", "review_case", "timeline_case"]


def movie(client, final):
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(final["output_file_id"]))
        assert file is not None
        return ObjectStorage(client.app.state.settings).read(
            file.object_key, file.byte_size, file.checksum
        )


def registry_counts(db):
    return tuple(
        db.scalar(select(func.count(model.id)))
        for model in (
            VisualAsset,
            AssetVersion,
            StoredFile,
            RenderArtifact,
            DependencyManifest,
            DependencyOutput,
            AssetDependency,
        )
    )


def exact_edges(db, version_id):
    return {
        (str(edge.source_asset_version_id), edge.dependency_type): edge.dependency_manifest_id
        for edge in db.scalars(
            select(AssetDependency).where(AssetDependency.target_asset_version_id == version_id)
        )
    }


def test_worker_registers_exact_version_file_inspection_and_all_dependencies(client, job_case):
    manifest, value, _ = job_case
    frozen = manifest["snapshot"]
    assert run(client, value) == "SUCCEEDED"
    final = read(client, value)
    assert final["output_asset_version_id"] and final["artifact_id"]
    assert final["output_file_id"] and final["is_delivery"] is False
    assert final["status"] == "SUCCEEDED" and final["cleanup_status"] == "RETAINED"
    with client.app.state.session_factory() as db:
        artifact = db.get(RenderArtifact, UUID(final["artifact_id"]))
        version = db.get(AssetVersion, UUID(final["output_asset_version_id"]))
        file = db.get(StoredFile, UUID(final["output_file_id"]))
        asset = db.get(VisualAsset, version.asset_id)
        attempt = db.scalar(select(RenderAttempt).where(RenderAttempt.run_id == UUID(value["id"])))
        inspection = db.scalar(
            select(RenderInspection).where(RenderInspection.attempt_id == attempt.id)
        )
        dependency_id = UUID(version.metadata_json["dependency_manifest_id"])
        receipt = db.scalar(
            select(DependencyOutput).where(DependencyOutput.asset_version_id == version.id)
        )
        dependencies = db.get(DependencyManifest, dependency_id)
        assert artifact.job_id == UUID(value["id"])
        assert artifact.manifest_id == UUID(manifest["id"])
        assert artifact.asset_version_id == file.asset_version_id == version.id
        assert artifact.file_id == file.id
        assert version.id == uuid5(UUID(value["id"]), "render-version")
        assert asset.id == uuid5(UUID(frozen["timeline"]["episode_id"]), "dependency:RENDER")
        assert asset.asset_type == "RENDER" and asset.current_version_id is None
        assert version.version_no == 1 and version.status == "DRAFT" and not version.stale
        assert artifact.object_key == file.object_key == version.object_key == attempt.output_key
        assert artifact.checksum == file.checksum == version.checksum == inspection.output_sha256
        assert artifact.byte_size == file.byte_size == inspection.byte_size
        assert file.original_name == "render-v1.mp4" and file.media_type == "video/mp4"
        assert file.rights_confirmed and file.created_by == version.created_by
        assert inspection.passed and inspection.input_sha256 == attempt.input_sha256
        assert inspection.manifest_sha256 == manifest["content_sha256"]
        measured = inspection.result_json["measurement"]["video"]["duration_us"]
        assert artifact.duration_ms == (measured + 500) // 1000 == 2500
        assert version.metadata_json == {
            "episode_id": frozen["timeline"]["episode_id"],
            "render_registry": True,
            "render_job_id": value["id"],
            "render_manifest_id": manifest["id"],
            "render_manifest_sha256": manifest["content_sha256"],
            "timeline_id": frozen["timeline_id"],
            "timeline_sha256": frozen["timeline_sha256"],
            "dependency_manifest_id": str(dependency_id),
            "inspection_id": str(inspection.id),
            "profile": frozen["output"]["name"],
            "storage_backend": file.storage_backend,
            "storage_bucket": file.bucket,
            "media": {
                "media_type": "video/mp4",
                "byte_size": file.byte_size,
                "duration_ms": artifact.duration_ms,
            },
        }
        assert receipt.dependency_manifest_id == dependency_id
        expected = {
            (frozen["timeline_id"], "RENDER_DEPENDENCY"),
            (frozen["timeline_id"], "SEQUENCE_DEPENDENCY"),
        }
        for source in frozen["sources"]:
            kind = {
                "SCRIPT": "CONTENT_DEPENDENCY",
                "STORYBOARD": "SEQUENCE_DEPENDENCY",
            }.get(source["asset_type"], "RENDER_DEPENDENCY")
            expected.add((source["asset_version_id"], kind))
        assert exact_edges(db, version.id) == dict.fromkeys(expected, dependency_id)
        captured = dependencies.snapshot_json
        assert {
            (item["source_asset_version_id"], item["dependency_type"])
            for item in captured["inputs"]
        } == expected
        for item in captured["inputs"]:
            source = db.get(AssetVersion, UUID(item["source_asset_version_id"]))
            assert item["checksum"] == source.checksum
            assert item["asset_id"] == str(source.asset_id)
        assert captured["context"] == {
            "episode_id": frozen["timeline"]["episode_id"],
            "render_manifest_id": manifest["id"],
            "render_manifest_sha256": manifest["content_sha256"],
            "render_job_id": value["id"],
            "timeline_id": frozen["timeline_id"],
            "font_file_id": frozen["font"]["file_id"] if frozen["font"] else None,
            "software": frozen["software"],
        }
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == "render.version_registered"
                )
            )
            == 1
        )
    assert sha256(movie(client, final)).hexdigest() == artifact.checksum


def test_replay_and_same_manifest_rerender_keep_prior_versions_and_input_bytes(client, job_case):
    manifest, value, _ = job_case
    factory, settings = client.app.state.session_factory, client.app.state.settings
    with factory() as db:
        inputs = list(db.scalars(select(StoredFile)))
        original_bytes = {
            row.id: ObjectStorage(settings).read(row.object_key, row.byte_size, row.checksum)
            for row in inputs
        }
    assert run(client, value) == "SUCCEEDED"
    first = read(client, value)
    original_movie = movie(client, first)
    with factory() as db:
        counts = registry_counts(db)
        old_version = db.get(AssetVersion, UUID(first["output_asset_version_id"]))
        original_metadata = deepcopy(old_version.metadata_json)
        original_edges = exact_edges(db, old_version.id)
    assert run(client, value) == "SUCCEEDED"
    assert read(client, value) == first
    with factory() as db:
        assert registry_counts(db) == counts
    another, _ = submit(client, manifest)
    assert run(client, another) == "SUCCEEDED"
    second = read(client, another)
    assert first["artifact_id"] != second["artifact_id"]
    assert first["output_file_id"] != second["output_file_id"]
    assert first["output_asset_version_id"] != second["output_asset_version_id"]
    with factory() as db:
        old = db.get(AssetVersion, UUID(first["output_asset_version_id"]))
        new = db.get(AssetVersion, UUID(second["output_asset_version_id"]))
        assert old.asset_id == new.asset_id and (old.version_no, new.version_no) == (1, 2)
        assert old.object_key != new.object_key
        assert old.metadata_json == original_metadata
        assert exact_edges(db, old.id) == original_edges
        assert (
            new.metadata_json["dependency_manifest_id"]
            != original_metadata["dependency_manifest_id"]
        )
        assert {pair for pair in exact_edges(db, new.id)} == set(original_edges)
        assert db.get(VisualAsset, new.asset_id).current_version_id is None
        for row in inputs:
            assert (
                ObjectStorage(settings).read(row.object_key, row.byte_size, row.checksum)
                == original_bytes[row.id]
            )
    assert movie(client, first) == original_movie
    assert read(client, value) == first
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest


def test_saved_timeline_rerender_appends_version_without_replacing_old_film(
    client, job_case, render_case
):
    manifest, value, _ = job_case
    assert run(client, value) == "SUCCEEDED"
    first = read(client, value)
    original_movie = movie(client, first)
    changed = duration_edit({"snapshot": render_case["snapshot"]}, 600)["snapshot"]
    timeline = edited(client, render_case, changed)
    new_manifest, _ = ready_manifest(client, timeline)
    another, _ = submit(client, new_manifest)
    assert run(client, another) == "SUCCEEDED"
    second = read(client, another)
    with client.app.state.session_factory() as db:
        old = db.get(AssetVersion, UUID(first["output_asset_version_id"]))
        new = db.get(AssetVersion, UUID(second["output_asset_version_id"]))
        assert old.asset_id == new.asset_id and (old.version_no, new.version_no) == (1, 2)
        assert old.metadata_json["timeline_id"] == render_case["id"]
        assert new.metadata_json["timeline_id"] == timeline["id"] != render_case["id"]
        assert old.metadata_json["render_manifest_id"] == manifest["id"]
        assert new.metadata_json["render_manifest_id"] == new_manifest["id"] != manifest["id"]
        assert (render_case["id"], "RENDER_DEPENDENCY") in exact_edges(db, old.id)
        assert (timeline["id"], "RENDER_DEPENDENCY") in exact_edges(db, new.id)
        assert (render_case["id"], "RENDER_DEPENDENCY") not in exact_edges(db, new.id)
        assert new.metadata_json["media"]["duration_ms"] == 3000
    assert movie(client, first) == original_movie
    assert client.get(f"/api/v1/timelines/{render_case['id']}").json() == render_case
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest
    with av.open(BytesIO(movie(client, second))) as container:
        assert len(list(container.decode(video=0))) == 72


def test_publication_failure_rolls_back_entire_registry_but_keeps_inspection(
    client, job_case, monkeypatch
):
    _, value, _ = job_case
    factory, settings = client.app.state.session_factory, client.app.state.settings
    with factory() as db:
        counts = registry_counts(db)
    register = render_execution.register_render_version
    reached = []

    def fail_after_registration(*args, **kwargs):
        artifact = register(*args, **kwargs)
        reached.append(artifact.id)
        raise RuntimeError("injected failure after complete registry registration")

    monkeypatch.setattr(render_execution, "register_render_version", fail_after_registration)
    assert run(client, value) == "FAILED"
    assert len(reached) == 1
    final = read(client, value)
    assert final["error_code"] == "RENDER_FAILED" and final["cleanup_status"] == "CLEANED"
    assert (
        final["output_file_id"] is final["output_asset_version_id"] is final["artifact_id"] is None
    )
    with factory() as db:
        assert registry_counts(db) == counts
        attempt = db.scalar(select(RenderAttempt).where(RenderAttempt.run_id == UUID(value["id"])))
        inspection = db.scalar(
            select(RenderInspection).where(RenderInspection.attempt_id == attempt.id)
        )
        assert inspection.passed and inspection.input_sha256 == attempt.input_sha256
        assert db.get(RenderArtifact, reached[0]) is None
        assert not db.scalar(
            select(AuditEvent.id).where(AuditEvent.action == "render.version_registered")
        )
        assert snapshot(ObjectStorage(settings), attempt.output_key) is None
        execution = db.get(RenderExecution, UUID(value["id"]))
        input_file = db.get(StoredFile, execution.input_file_id)
        assert snapshot(ObjectStorage(settings), input_file.object_key) is not None


@pytest.mark.parametrize("kind,change_kind", [("TIMELINE", "SEQUENCE"), ("SCRIPT", "DIALOGUE")])
def test_exact_upstream_change_stales_real_render_without_rewriting_history(
    client, job_case, kind, change_kind
):
    manifest, value, _ = job_case
    assert run(client, value) == "SUCCEEDED"
    final = read(client, value)
    original_movie = movie(client, final)
    frozen = manifest["snapshot"]
    source_id = (
        frozen["timeline_id"]
        if kind == "TIMELINE"
        else next(
            item["asset_version_id"] for item in frozen["sources"] if item["asset_type"] == kind
        )
    )
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(final["output_asset_version_id"]))
        original_metadata, edges = deepcopy(version.metadata_json), exact_edges(db, version.id)
        source = db.get(AssetVersion, UUID(source_id))
        event, created = apply(
            db, db.get(VisualAsset, version.asset_id).project_id, source, change_kind
        )
        assert created and str(version.id) in {
            item["asset_version_id"] for item in event.snapshot_json["affected"]
        }
        db.refresh(version)
        assert version.stale and version.status == "STALE"
        assert version.metadata_json == original_metadata and exact_edges(db, version.id) == edges
    assert movie(client, final) == original_movie
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest


def test_internal_render_inputs_capture_more_than_public_limit_and_invalidate(client, episode):
    """Exercise exact registry fixtures; this is not a 65-shot media render."""
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        sources = [registry_version(db, pid, kind="SCRIPT") for _ in range(65)]
        target = VisualAsset(project_id=pid, asset_type="RENDER", name="large render manifest")
        db.add(target)
        db.flush()
        inputs = [
            DependencyInput(
                source_asset_version_id=version.id, dependency_type="CONTENT_DEPENDENCY"
            )
            for _, version in sources
        ]
        body = {"creation_key": uuid4(), "target_asset_id": target.id, "inputs": inputs}
        with pytest.raises(ValidationError):
            ManifestCreate(**body)
        assert len(ManifestCreate(**{**body, "inputs": inputs[:64]}).inputs) == 64
        boundary_inputs = [
            DependencyInput(source_asset_version_id=uuid4(), dependency_type="CONTENT_DEPENDENCY")
            for _ in range(4097)
        ]
        assert len(RenderInputs(**{**body, "inputs": boundary_inputs[:2049]}).inputs) == 2049
        assert len(RenderInputs(**{**body, "inputs": boundary_inputs[:4096]}).inputs) == 4096
        with pytest.raises(ValidationError):
            RenderInputs(**{**body, "inputs": boundary_inputs})
        manifest, created = capture_manifest(db, pid, "service-owner", RenderInputs(**body))
        assert created and len(manifest.snapshot_json["inputs"]) == 65
        version = AssetVersion(
            asset_id=target.id,
            version_no=1,
            checksum="c" * 64,
            object_key="registry-only-large-render",
            created_by="service-owner",
            metadata_json={"dependency_manifest_id": str(manifest.id)},
        )
        db.add(version)
        db.flush()
        attach_output(db, pid, manifest.id, version)
        assert exact_edges(db, version.id) == {
            (str(source.id), "CONTENT_DEPENDENCY"): manifest.id for _, source in sources
        }
        event, _ = apply(db, pid, sources[0][1], "DIALOGUE")
        assert str(version.id) in {
            item["asset_version_id"] for item in event.snapshot_json["affected"]
        }
        db.refresh(version)
        assert version.stale


def test_only_registered_sealed_timeline_can_be_draft_render_input(client, render_case):
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(render_case["id"]))
        asset = db.get(VisualAsset, version.asset_id)
        timeline = db.get(Timeline, version.id)
        assert timeline.sealed and version.status == "DRAFT"
        assert input_usable(db, version, asset, "RENDER")
        assert not input_usable(db, version, asset, "TIMELINE")
        assert not input_usable(db, version, asset, "SHOT_PREVIEW")
        pid = asset.project_id
        draft_asset, draft = registry_version(db, pid, kind="SHOT_IMAGE", status="DRAFT")
        assert not input_usable(db, draft, draft_asset, "RENDER")
        approved_asset, approved = registry_version(db, pid, kind="SHOT_IMAGE")
        assert input_usable(db, approved, approved_asset, "RENDER")
        snapshot = TimelineSnapshot.model_validate(render_case["snapshot"])
        for index, (sealed, attached) in enumerate([(False, True), (True, False)], 1):
            fake_asset = asset
            fake = AssetVersion(
                asset_id=fake_asset.id,
                version_no=timeline.version_no + index,
                checksum="a" * 64,
                object_key="registry-only-timeline:" + str(uuid4()),
                created_by="service-owner",
                status="DRAFT",
                metadata_json={
                    **version.metadata_json,
                    "dependency_manifest_id": str(uuid4()),
                },
            )
            db.add(fake)
            db.flush()
            manifest = frozen_manifest(db, pid, fake_asset, fake, [])
            if attached:
                attach_output(db, pid, manifest.id, fake)
            assert not input_usable(db, fake, fake_asset, "RENDER")
            candidate = Timeline(
                id=fake.id,
                episode_id=timeline.episode_id,
                storyboard_id=timeline.storyboard_id,
                version_no=fake.version_no,
                default_duration_ms=500,
                duration_ms=timeline.duration_ms,
                output_profile_json=timeline.output_profile_json,
                creation_key=uuid4(),
                request_sha256="d" * 64,
                created_by="service-owner",
            )
            db.add(candidate)
            db.flush()
            # A legal seal follows complete tracks, clips and visual settings.
            for track in snapshot.tracks:
                track_id = uuid4()
                db.add(
                    TimelineTrack(
                        id=track_id,
                        timeline_id=candidate.id,
                        kind=track.kind,
                        ordinal=track.ordinal,
                    )
                )
                db.flush()
                for clip in track.clips:
                    clip_id = uuid4()
                    db.add(
                        TimelineClip(
                            id=clip_id,
                            timeline_id=candidate.id,
                            track_id=track_id,
                            **clip.model_dump(exclude={"id", "visual"}),
                        )
                    )
                    db.flush()
                    if clip.visual is not None:
                        db.add(
                            TimelineClipVisual(
                                id=clip_id,
                                timeline_id=candidate.id,
                                settings_json=clip.visual.model_dump(mode="json"),
                            )
                        )
                        db.flush()
            if sealed:
                candidate.sealed = True
                db.flush()
            receipt = db.scalar(
                select(DependencyOutput).where(DependencyOutput.asset_version_id == fake.id)
            )
            assert candidate.sealed is sealed and (receipt is not None) is attached
            assert not input_usable(db, fake, fake_asset, "RENDER")
        version.status = "READY_FOR_REVIEW"
        db.flush()
        assert input_usable(db, version, asset, "RENDER")
        with db.begin_nested() as savepoint:
            version.stale = True
            db.flush()
            assert not input_usable(db, version, asset, "RENDER")
            savepoint.rollback()
        asset.status = "ARCHIVED"
        db.flush()
        assert not input_usable(db, version, asset, "RENDER")


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"dependency_manifest_id": None},
        {"dependency_manifest_id": ""},
        {"dependency_manifest_id": "invalid-uuid"},
        {"dependency_manifest_id": 12},
        {"dependency_manifest_id": []},
        {"dependency_manifest_id": {}},
    ],
    ids=["missing", "null", "empty", "invalid-uuid", "integer", "list", "object"],
)
def test_draft_timeline_with_invalid_manifest_metadata_is_not_usable(client, episode, metadata):
    with client.app.state.session_factory.begin() as db:
        asset = VisualAsset(
            project_id=UUID(episode["project_id"]), asset_type="TIMELINE", name="invalid input"
        )
        db.add(asset)
        db.flush()
        version = AssetVersion(
            asset_id=asset.id,
            version_no=1,
            checksum="a" * 64,
            object_key="registry-only-invalid-timeline",
            created_by="service-owner",
            status="DRAFT",
            metadata_json=metadata,
        )
        db.add(version)
        db.flush()
        assert not input_usable(db, version, asset, "RENDER")
