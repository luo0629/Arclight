"""Real candidate inspections remain fenced, private and durable across failures."""

import hashlib
from uuid import UUID

import pytest
from render_inspection_helpers import movie
from render_job_helpers import (
    cancel,
    expire,
    job_case,
    read,
    render_case,
    review_case,
    run,
    submit,
    timeline_case,
)
from sqlalchemy import select
from test_collaboration import register
from test_media_execution import recorded_children

from arclight.application import render_execution, render_inspections
from arclight.application.render_leases import claim
from arclight.domain.render_inspections import RenderInspectionResult
from arclight.persistence.models import (
    AssetVersion,
    Project,
    ProjectMember,
    RenderAttempt,
    RenderInspection,
    StoredFile,
)
from arclight.storage.cleanup import snapshot
from arclight.storage.objects import ObjectStorage

__all__ = ["job_case", "render_case", "review_case", "timeline_case", "recorded_children"]


def history(client, value):
    response = client.get(f"/api/v1/render-jobs/{value['id']}/attempts")
    assert response.status_code == 200, response.text
    return response.json()


def test_passed_receipt_binds_exact_file_manifest_inputs_and_authorized_api(client, job_case):
    manifest, value, _ = job_case
    assert run(client, value) == "SUCCEEDED"
    final = read(client, value)
    rows = history(client, value)
    report = rows[0]["inspection"]
    assert report["passed"] and report["error_code"] is None
    assert report["measurement"]["video"]["frames"] == 60
    assert report["measurement"]["video"]["duration_us"] == 2_500_000
    with client.app.state.session_factory() as db:
        receipt = db.scalar(select(RenderInspection))
        attempt = db.get(RenderAttempt, receipt.attempt_id)
        output = db.get(StoredFile, UUID(final["output_file_id"]))
        assert receipt.manifest_id == UUID(manifest["id"])
        assert receipt.manifest_sha256 == manifest["content_sha256"]
        assert receipt.input_sha256 == attempt.input_sha256
        assert receipt.output_sha256 == output.checksum == report["output_sha256"]
        assert receipt.byte_size == output.byte_size == report["byte_size"]
        restored = RenderInspectionResult.model_validate(receipt.result_json)
        assert restored.model_dump(mode="json") == report
        data = ObjectStorage(client.app.state.settings).read(
            output.object_key, output.byte_size, output.checksum
        )
        assert hashlib.sha256(data).hexdigest() == receipt.output_sha256
        assert attempt.output_key not in str(rows)
    member = register(client, "inspection-viewer@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(manifest["project_id"]),
                user_id=UUID(member["user"]["id"]),
                role="viewer",
            )
        )
    assert history(client, value) == rows
    register(client, "inspection-other-project@example.com")
    project = client.post("/api/v1/projects", json={"name": "Other project"})
    assert project.status_code == 201
    assert client.get(f"/api/v1/render-jobs/{value['id']}/attempts").status_code == 404
    client.headers["Authorization"] = "Bearer test-token"
    assert run(client, value) == "SUCCEEDED"
    assert history(client, value) == rows


@pytest.mark.parametrize(
    "candidate,code",
    [
        ("damaged", "RENDER_INSPECTION_INVALID_MEDIA"),
        ("wrong_size", "RENDER_OUTPUT_RESOLUTION"),
        ("short_video", "RENDER_OUTPUT_DURATION"),
        ("short_audio", "RENDER_OUTPUT_DURATION"),
    ],
)
def test_rejected_candidate_never_reaches_store_and_retry_keeps_failed_evidence(
    client, job_case, monkeypatch, candidate, code
):
    manifest, value, _ = job_case
    changes = {"frames": 60}
    if candidate == "wrong_size":
        changes["width"] = 128
    elif candidate == "short_video":
        changes["frames"] = 59
    elif candidate == "short_audio":
        changes["audio_frames"] = 48
    data = b"PRIVATE_NATIVE_ERROR:/private/source" if candidate == "damaged" else movie(**changes)
    writes = []
    original_put = ObjectStorage.put

    def put(store, key, *args, **kwargs):
        if key.startswith("rendered/"):
            writes.append(key)
        return original_put(store, key, *args, **kwargs)

    monkeypatch.setattr(ObjectStorage, "put", put)
    with monkeypatch.context() as patch:
        patch.setattr(render_execution, "execute_media", lambda *args, **kwargs: data)
        assert run(client, value) == "FAILED"
    failed = read(client, value)
    evidence = history(client, value)
    report = evidence[0]["inspection"]
    assert failed["error_code"] == report["error_code"] == code
    assert not report["passed"] and failed["output_file_id"] is None
    assert failed["cleanup_status"] == "CLEANED" and writes == []
    assert report["output_sha256"] == hashlib.sha256(data).hexdigest()
    assert report["byte_size"] == len(data) and "PRIVATE_NATIVE_ERROR" not in str(evidence)
    with client.app.state.session_factory() as db:
        attempt = db.scalar(select(RenderAttempt))
        assert snapshot(ObjectStorage(client.app.state.settings), attempt.output_key) is None
        assert not list(
            db.scalars(select(StoredFile).where(StoredFile.original_name == "render-candidate.mp4"))
        )
    retry, _ = submit(client, manifest, retry_of=value["id"])
    assert run(client, retry) == "SUCCEEDED"
    assert history(client, value) == evidence and read(client, value) == failed
    assert history(client, retry)[0]["inspection"]["passed"] and len(writes) == 1


def test_cancel_during_inspection_kills_actual_child_and_leaves_no_published_candidate(
    client, job_case, monkeypatch, recorded_children
):
    _, value, _ = job_case
    capture, children, _, _ = recorded_children
    original = render_inspections.execute_media

    def inspect(*args, **kwargs):
        assert read(client, value)["phase"] == "INSPECTING"
        capture("import sys,time; sys.stdin.buffer.read(); time.sleep(30)")
        observer, cancelled = kwargs["observer"], []

        def observe(progress):
            if not cancelled:
                assert cancel(client, value).status_code == 200
                cancelled.append(True)
            observer(progress)

        return original(*args, **{**kwargs, "observer": observe})

    monkeypatch.setattr(render_inspections, "execute_media", inspect)
    assert run(client, value) == "CANCELLED"
    final = read(client, value)
    assert final["output_file_id"] is None and final["cleanup_status"] == "CLEANED"
    assert history(client, value)[0]["inspection"] is None
    assert children[-1].poll() is not None and children[-1].returncode != 0


@pytest.mark.parametrize(
    "change,code,has_receipt",
    [
        ("project", "PROJECT_ARCHIVED", False),
        ("stale", "ASSET_NOT_USABLE", False),
        ("permission", "FORBIDDEN", False),
        ("object", "OBJECT_UNAVAILABLE", True),
        ("cancel", None, False),
    ],
)
def test_inspection_completion_rechecks_current_scope_and_exact_inputs(
    client, job_case, monkeypatch, change, code, has_receipt
):
    manifest, value, _ = job_case
    factory = client.app.state.session_factory
    member = register(client, "inspection-creator@example.com")
    with factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(manifest["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="editor",
        )
        db.add(membership)
        db.flush()
        membership_id = membership.id
    value, _ = submit(client, manifest)
    original = render_execution.inspect_candidate

    def inspect(*args, **kwargs):
        report = original(*args, **kwargs)
        assert report.passed and read(client, value)["phase"] == "INSPECTING"
        if change == "cancel":
            assert cancel(client, value).status_code == 200
        else:
            with factory.begin() as db:
                if change == "project":
                    db.get(Project, UUID(manifest["project_id"])).archived = True
                elif change == "stale":
                    source = next(
                        item
                        for item in manifest["snapshot"]["sources"]
                        if item["asset_type"] in {"IMAGE", "SHOT_IMAGE"}
                    )
                    db.get(AssetVersion, UUID(source["asset_version_id"])).stale = True
                elif change == "permission":
                    db.get(ProjectMember, membership_id).role = "viewer"
                else:
                    source = next(item for item in manifest["snapshot"]["sources"] if item["file"])
                    file = db.get(StoredFile, UUID(source["file"]["file_id"]))
                    ObjectStorage(client.app.state.settings).delete(file.object_key)
        return report

    monkeypatch.setattr(render_execution, "inspect_candidate", inspect)
    assert run(client, value) == ("CANCELLED" if change == "cancel" else "FAILED")
    client.headers["Authorization"] = "Bearer test-token"
    final = read(client, value)
    assert final["error_code"] == code and final["output_file_id"] is None
    assert (history(client, value)[0]["inspection"] is not None) == has_receipt


@pytest.mark.parametrize("replacement", [False, True])
def test_expired_inspector_cannot_attach_evidence_or_publish_for_replacement(
    client, job_case, monkeypatch, replacement
):
    _, value, _ = job_case
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    original = render_execution.inspect_candidate

    def inspect(*args, **kwargs):
        report = original(*args, **kwargs)
        expire(client, value)
        if replacement:
            assert claim(factory, run_id)
        return report

    with monkeypatch.context() as patch:
        patch.setattr(render_execution, "inspect_candidate", inspect)
        assert run(client, value) == "RUNNING"
    assert read(client, value)["output_file_id"] is None
    assert all(row["inspection"] is None for row in history(client, value))
    expire(client, value)
    assert run(client, value) == "SUCCEEDED"
    rows = history(client, value)
    assert all(row["status"] == "INTERRUPTED" and row["inspection"] is None for row in rows[:-1])
    assert rows[-1]["inspection"]["passed"]


def test_crash_after_inspection_retains_old_receipt_and_recovery_appends_new_attempt(
    client, job_case, monkeypatch
):
    _, value, _ = job_case
    original_put = ObjectStorage.put

    def crash(store, key, *args, **kwargs):
        if key.startswith("rendered/"):
            raise SystemExit("simulated worker loss")
        return original_put(store, key, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ObjectStorage, "put", crash)
        with pytest.raises(SystemExit, match="simulated worker loss"):
            run(client, value)
    old_report = history(client, value)[0]["inspection"]
    assert old_report["passed"] and read(client, value)["status"] == "RUNNING"
    expire(client, value)
    assert run(client, value) == "SUCCEEDED"
    rows = history(client, value)
    assert [row["status"] for row in rows] == ["INTERRUPTED", "SUCCEEDED"]
    assert rows[0]["inspection"] == old_report and rows[1]["inspection"]["passed"]
