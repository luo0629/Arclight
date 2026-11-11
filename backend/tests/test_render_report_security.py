"""Delivery authority cannot come from borrowed reports, revoked users, or stale proof."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from render_job_helpers import read, run, submit
from render_manifest_helpers import report_bytes as legacy_report_bytes
from render_manifest_helpers import report_case, result_case
from render_report_helpers import (
    accept_warnings,
    delivery_url,
    formal_report_case,
    production_state,
    render_case,
    report_bytes,
    report_read,
    report_url,
    request_warnings,
    review_case,
    timeline_case,
)
from sqlalchemy import func, select
from test_collaboration import register
from test_human_reviews import decide, decision_payload, resume

from arclight.application.render_reports import report_warning, report_warnings
from arclight.domain.render_manifests import LineAudioDecision, RenderManifestSnapshot
from arclight.domain.render_reports import RenderMachineReport, RenderWarning
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewResume
from arclight.persistence.models import AssetVersion, Project, ProjectMember, StoredFile
from arclight.persistence.render_manifests import RenderReportLink
from arclight.storage.objects import ObjectStorage

__all__ = [
    "formal_report_case",
    "render_case",
    "report_case",
    "result_case",
    "review_case",
    "timeline_case",
]


def member(client, episode, role):
    owner = client.headers["Authorization"]
    account = register(client, f"delivery-report-{role}@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(episode["project_id"]),
                user_id=UUID(account["user"]["id"]),
                role=role,
            )
        )
    return owner, account


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_members_can_read_exact_report_but_only_owner_accepts_exceptions(
    client, episode, formal_report_case, role
):
    _, final, report = formal_report_case
    requested = request_warnings(client, episode, final["artifact_id"])
    with client.app.state.session_factory() as db:
        prior_decisions = {
            row.id: deepcopy(row.decision_json) for row in db.scalars(select(HumanReviewDecision))
        }
    owner, _ = member(client, episode, role)
    assert report_read(client, episode, final["artifact_id"])["report"] == report["report"]
    assert (
        client.post(delivery_url(episode, final["artifact_id"]) + "/warning-review").status_code
        == 403
    )
    assert decide(client, requested).status_code == 403
    client.headers["Authorization"] = owner
    assert (
        report_read(client, episode, final["artifact_id"])["acceptance"]["phase"] == "DELIVERY_QA"
    )
    with client.app.state.session_factory() as db:
        assert {
            row.id: row.decision_json for row in db.scalars(select(HumanReviewDecision))
        } == prior_decisions
        assert (
            db.scalar(
                select(func.count(HumanReviewDecision.id)).where(
                    HumanReviewDecision.request_id == UUID(requested["id"])
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(HumanReviewResume.id)).where(
                    HumanReviewResume.request_id == UUID(requested["id"])
                )
            )
            == 0
        )


def test_report_scope_and_membership_are_rechecked_on_every_read(
    client, episode, formal_report_case
):
    _, final, _ = formal_report_case
    owner = client.headers["Authorization"]
    created = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "其他剧集"},
    )
    assert created.status_code == 201, created.text
    other_episode = created.json()
    assert client.get(report_url(other_episode, final["artifact_id"])).status_code == 404
    assert (
        client.post(
            delivery_url(other_episode, final["artifact_id"]) + "/warning-review"
        ).status_code
        == 404
    )
    register(client, "delivery-report-outsider@example.com")
    assert client.get(report_url(episode, final["artifact_id"])).status_code == 404
    assert (
        client.post(delivery_url(episode, final["artifact_id"]) + "/warning-review").status_code
        == 404
    )
    client.headers["Authorization"] = owner
    owner, account = member(client, episode, "viewer")
    assert client.get(report_url(episode, final["artifact_id"])).status_code == 200
    client.headers["Authorization"] = owner
    removed = client.delete(
        f"/api/v1/projects/{episode['project_id']}/members/{account['user']['id']}"
    )
    assert removed.status_code == 204, removed.text
    client.headers["Authorization"] = "Bearer " + account["access_token"]
    assert client.get(report_url(episode, final["artifact_id"])).status_code == 404
    client.cookies.clear()
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get(report_url(episode, final["artifact_id"])).status_code == 401


def test_warning_review_freezes_the_complete_set_and_refuses_client_subset(
    client, episode, formal_report_case
):
    _, final, report = formal_report_case
    details = report["report"]["details"]
    warning_ids = [warning["id"] for warning in details["warnings"]]
    assert len(warning_ids) > 1
    requested = request_warnings(client, episode, final["artifact_id"])
    assert requested["snapshot"]["input_sha256"] == digest(
        {
            "input_sha256": details["input_sha256"],
            "report_sha256": report["report_sha256"],
            "warning_ids": warning_ids,
        }
    )
    payload = decision_payload(requested) | {"warning_ids": warning_ids[:1]}
    refused = client.post(f"/api/v1/human-reviews/{requested['id']}/decisions", json=payload)
    assert refused.status_code == 422, refused.text
    assert report_read(client, episode, final["artifact_id"])["acceptance"]["is_delivery"] is False
    accepted = decide(client, requested)
    assert accepted.status_code == 200, accepted.text
    assert resume(client, requested, accepted.json()).status_code == 200
    assert report_read(client, episode, final["artifact_id"])["acceptance"]["is_delivery"] is True


@pytest.mark.parametrize("field", ["version_id", "request_sha256", "comment"])
def test_warning_decision_rejects_wrong_version_hash_or_blank_consent(
    client, episode, formal_report_case, field
):
    _, final, _ = formal_report_case
    requested = request_warnings(client, episode, final["artifact_id"])
    payload = decision_payload(requested)
    payload[field] = (
        str(uuid4()) if field == "version_id" else "0" * 64 if field == "request_sha256" else " \t "
    )
    response = client.post(f"/api/v1/human-reviews/{requested['id']}/decisions", json=payload)
    assert response.status_code in {409, 422}, response.text
    assert not report_read(client, episode, final["artifact_id"])["acceptance"]["is_delivery"]
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count(HumanReviewDecision.id)).where(
                    HumanReviewDecision.request_id == UUID(requested["id"])
                )
            )
            == 0
        )


def test_warning_decision_cannot_be_borrowed_by_same_manifest_rerender(
    client, episode, formal_report_case
):
    manifest, first, original = formal_report_case
    first_request, decision, _ = accept_warnings(client, episode, first["artifact_id"])
    next_job, _ = submit(client, manifest)
    assert run(client, next_job) == "SUCCEEDED"
    second = read(client, next_job)
    request = request_warnings(client, episode, second["artifact_id"])
    assert request["id"] != first_request["id"]
    assert request["snapshot"]["input_sha256"] != first_request["snapshot"]["input_sha256"]
    assert request["snapshot"]["content_sha256"] != original["report_sha256"]
    borrowed = resume(client, request, decision)
    assert borrowed.status_code == 409, borrowed.text
    assert not report_read(client, episode, second["artifact_id"])["acceptance"]["is_delivery"]
    assert report_read(client, episode, first["artifact_id"])["acceptance"]["is_delivery"]


@pytest.mark.parametrize("moment", ["before_decision", "before_resume", "after_resume"])
def test_stale_render_invalidates_pending_and_accepted_warnings_without_rewriting_report(
    client, episode, formal_report_case, moment
):
    _, final, original = formal_report_case
    original_bytes = report_bytes(client, original)
    requested = request_warnings(client, episode, final["artifact_id"])
    saved = None
    if moment != "before_decision":
        response = decide(client, requested)
        assert response.status_code == 200, response.text
        saved = response.json()
    if moment == "after_resume":
        assert resume(client, requested, saved).status_code == 200
        assert report_read(client, episode, final["artifact_id"])["acceptance"]["is_delivery"]
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(final["output_asset_version_id"])).stale = True
    if moment == "before_decision":
        assert decide(client, requested).status_code == 409
    elif moment == "before_resume":
        assert resume(client, requested, saved).status_code == 409
    current = report_read(client, episode, final["artifact_id"])
    assert current["acceptance"]["phase"] == "DELIVERY_QA"
    assert current["acceptance"]["is_delivery"] is False
    assert current["report"] == original["report"]
    assert report_bytes(client, current) == original_bytes
    assert production_state(client, episode)["phase"] not in {
        "COMPLETED",
        "COMPLETED_WITH_WARNINGS",
    }


@pytest.mark.parametrize("moment", ["before_resume", "after_resume"])
def test_new_owner_cannot_resume_or_inherit_revoked_owners_warning_decision(
    client, episode, formal_report_case, moment
):
    _, final, _ = formal_report_case
    requested = request_warnings(client, episode, final["artifact_id"])
    saved = decide(client, requested)
    assert saved.status_code == 200, saved.text
    if moment == "after_resume":
        assert resume(client, requested, saved.json()).status_code == 200
        assert production_state(client, episode)["phase"] == "COMPLETED_WITH_WARNINGS"
    replacement = register(client, "delivery-report-new-owner@example.com")
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).owner_id = replacement["user"]["id"]
    assert resume(client, requested, saved.json()).status_code == 404
    current = report_read(client, episode, final["artifact_id"])
    assert current["acceptance"]["phase"] == "DELIVERY_QA"
    assert not current["acceptance"]["is_delivery"]
    assert production_state(client, episode)["phase"] not in {
        "COMPLETED",
        "COMPLETED_WITH_WARNINGS",
    }


@pytest.mark.parametrize("damage", ["missing", "same_length_replacement"])
@pytest.mark.parametrize(
    "moment", ["before_request", "before_decision", "before_resume", "after_resume"]
)
def test_machine_report_reads_require_actual_hash_checked_private_object(
    client, episode, formal_report_case, damage, moment
):
    _, final, report = formal_report_case
    requested = saved = None
    if moment != "before_request":
        requested = request_warnings(client, episode, final["artifact_id"])
    if moment in {"before_resume", "after_resume"}:
        response = decide(client, requested)
        assert response.status_code == 200, response.text
        saved = response.json()
    if moment == "after_resume":
        assert resume(client, requested, saved).status_code == 200
        assert production_state(client, episode)["phase"] == "COMPLETED_WITH_WARNINGS"
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(report["report_file_id"]))
        path = ObjectStorage(client.app.state.settings).path(file.object_key)
        expected_size = file.byte_size
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"x" * expected_size)
    response = client.get(report_url(episode, final["artifact_id"]))
    assert response.status_code == 409, response.text
    assert str(path) not in response.text
    refused = client.post(delivery_url(episode, final["artifact_id"]) + "/warning-review")
    assert refused.status_code == 409, refused.text
    if moment == "before_decision":
        refused = decide(client, requested)
        assert refused.status_code == 409, refused.text
    elif moment in {"before_resume", "after_resume"}:
        refused = resume(client, requested, saved)
        assert refused.status_code == 409, refused.text
        if moment == "after_resume":
            refused = decide(client, requested)
            assert refused.status_code == 409, refused.text
    current = production_state(client, episode)
    assert current["phase"] not in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}
    detail = client.get(delivery_url(episode, final["artifact_id"]))
    assert detail.status_code == 200, detail.text
    assert detail.json()["is_delivery"] is False
    assert detail.json()["acceptance"]["phase"] == "DELIVERY_QA"
    current_job = read(client, final)
    assert current_job["is_delivery"] is False
    assert current_job["delivery_phase"] == "DELIVERY_QA"
    assert current_job["report_id"] == report["report_id"]
    with client.app.state.session_factory() as db:
        link = db.get(RenderReportLink, UUID(report["report_id"]))
        assert link.checksum == report["report_sha256"]
        assert link.machine_report_json == report["report"]


def test_legacy_manual_report_does_not_gain_machine_or_delivery_authority(
    client, episode, report_case
):
    manifest, artifact, legacy = report_case
    file_id = legacy.file_id
    data = legacy_report_bytes(manifest, artifact)
    root = delivery_url(episode, str(artifact.id))
    assert client.get(root + "/report").status_code == 404
    refused = client.post(root + "/warning-review")
    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "NOT_FOUND"
    saved = client.get(f"/api/v1/render-manifests/{manifest['id']}/results")
    assert saved.status_code == 200, saved.text
    assert saved.json()["report"]["file_id"] == str(file_id)
    with client.app.state.session_factory() as db:
        link = db.scalar(select(RenderReportLink))
        assert link.machine_report_json is None and link.inspection_id is None
        file = db.get(StoredFile, file_id)
        assert (
            ObjectStorage(client.app.state.settings).read(file.object_key, len(data), file.checksum)
            == data
        )


def test_machine_report_schema_refuses_failed_missing_coerced_and_borrowed_checks(
    formal_report_case,
):
    _, _, response = formal_report_case
    original = response["report"]["details"]
    RenderMachineReport.model_validate(original)
    mutations = []
    for field, value in (
        ("video_decodable", False),
        ("audio_stream_present", 1),
        ("subtitle_range_valid", "true"),
        ("missing_required_shots", 1),
        ("missing_audio_lines", True),
        ("stale_dependencies", -1),
    ):
        changed = deepcopy(original)
        changed["checks"][field] = value
        mutations.append(changed)
    changed = deepcopy(original)
    del changed["checks"]["technical"]
    mutations.append(changed)
    changed = deepcopy(original)
    changed["checks"]["technical"]["output_sha256"] = "0" * 64
    mutations.append(changed)
    changed = deepcopy(original)
    changed["checks"]["technical"]["checks"][0]["passed"] = False
    mutations.append(changed)
    changed = deepcopy(original)
    changed["warnings"].append(deepcopy(changed["warnings"][0]))
    mutations.append(changed)
    for changed in mutations:
        with pytest.raises(ValidationError):
            RenderMachineReport.model_validate(changed)


def test_subtitle_only_exception_names_exact_line_and_cannot_forge_warning_identity():
    script = uuid4()
    warning = report_warning("SUBTITLE_ONLY", script, "line-1", "仅字幕表现该段内心独白")
    assert report_warning("SUBTITLE_ONLY", script, "line-1", warning.reason) == warning
    for source, line, reason in (
        (uuid4(), "line-1", warning.reason),
        (script, "line-2", warning.reason),
        (script, "line-1", "另一个创作决定"),
    ):
        changed = report_warning("SUBTITLE_ONLY", source, line, reason)
        assert changed.id != warning.id
        forged = changed.model_dump(mode="json") | {"id": warning.id}
        with pytest.raises(ValidationError):
            RenderWarning.model_validate(forged)
    with pytest.raises(ValidationError):
        report_warning("SUBTITLE_ONLY", script, None, warning.reason)


@pytest.mark.parametrize(
    ("mode", "code"), [("SILENT", "EXPLICIT_SILENCE"), ("SUBTITLE_ONLY", "SUBTITLE_ONLY")]
)
def test_typed_line_audio_decisions_preserve_exact_warning_provenance(
    client, formal_report_case, mode, code
):
    manifest, _, _ = formal_report_case
    snapshot_json = deepcopy(manifest["snapshot"])
    for decision in snapshot_json["line_audio_decisions"]:
        decision["mode"] = mode
    snapshot = RenderManifestSnapshot.model_validate(snapshot_json)
    assert snapshot.line_audio_decisions
    assert all(
        isinstance(decision, LineAudioDecision) for decision in snapshot.line_audio_decisions
    )
    assert all(decision.mode == mode for decision in snapshot.line_audio_decisions)
    with client.app.state.session_factory() as db:
        warnings = report_warnings(db, snapshot)
    assert len(warnings) == len(snapshot.line_audio_decisions)
    assert {warning.code for warning in warnings} == {code}
    assert {
        (warning.source_version_id, warning.line_id, warning.reason) for warning in warnings
    } == {
        (decision.script_version_id, decision.line_id, decision.reason)
        for decision in snapshot.line_audio_decisions
    }
    assert len({warning.id for warning in warnings}) == len(warnings)


def test_media_exception_retains_full_reason_and_exact_human_review_provenance():
    version = uuid4()
    provenance = {
        "human_review_request_id": uuid4(),
        "human_review_decision_id": uuid4(),
        "human_review_request_sha256": "a" * 64,
    }
    reason = " \t" + "允许此精确版本的已知视觉例外。" * 100 + "\n "
    warning = report_warning("MEDIA_EXCEPTION", version, None, reason, **provenance)
    assert 500 < len(reason) <= 2000
    assert warning.reason == reason
    for field, value in provenance.items():
        assert getattr(warning, field) == value
        missing = provenance | {field: None}
        with pytest.raises(ValidationError):
            report_warning("MEDIA_EXCEPTION", version, None, reason, **missing)
        changed = provenance | {
            field: "b" * 64 if field == "human_review_request_sha256" else uuid4()
        }
        replacement = report_warning("MEDIA_EXCEPTION", version, None, reason, **changed)
        assert replacement.id != warning.id
        forged = replacement.model_dump(mode="json") | {"id": warning.id}
        with pytest.raises(ValidationError):
            RenderWarning.model_validate(forged)
    with pytest.raises(ValidationError):
        report_warning("MEDIA_EXCEPTION", version, None, "例" * 2001, **provenance)
    with pytest.raises(ValidationError):
        report_warning("EXPLICIT_SILENCE", version, "line-1", "静音呈现", **provenance)
