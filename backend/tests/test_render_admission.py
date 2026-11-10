"""RENDER-08 live preflight uses current selection, QA and real immutable objects."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from render_admission_helpers import (
    assert_blocked,
    edited,
    ready_manifest,
    render_path,
    request_export,
)
from render_manifest_helpers import manifest, render_case, review_case, timeline_case
from sqlalchemy.exc import DBAPIError
from test_continuity_review_contracts import with_issue
from test_continuity_reviews import saved
from test_continuity_selection import admission_fixture
from test_human_reviews import decide, resume

from arclight.domain.versions import digest
from arclight.persistence.models import AssetVersion, Project, Script, Shot, StoredFile, VisualAsset
from arclight.storage.objects import ObjectStorage

__all__ = ["render_case", "review_case", "timeline_case"]


def test_preview_requires_explicit_decisions_for_all_dialogue_and_narration(client, render_case):
    value, _ = manifest(client, render_case)
    assert_blocked(client.post(render_path(value, "admission")), "RENDER_AUDIO_INCOMPLETE")


def test_ready_preview_is_an_observation_without_approval_or_delivery(client, render_case):
    value, _ = ready_manifest(client, render_case)
    response = client.post(render_path(value, "admission"))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "manifest_id": value["id"],
        "manifest_sha256": value["content_sha256"],
        "profile": "PREVIEW",
        "review_request_id": None,
        "decision_id": None,
        "checked_at": response.json()["checked_at"],
        "admitted": True,
        "requires_recheck": True,
        "is_delivery": False,
    }
    results = client.get(render_path(value, "results")).json()
    assert results["artifact"] is results["report"] is None


def test_formal_requires_the_exact_export_review(client, render_case):
    value, _ = ready_manifest(client, render_case, profile="FORMAL")
    assert_blocked(client.post(render_path(value, "admission")), "RENDER_EXPORT_REVIEW_REQUIRED")


@pytest.mark.parametrize(
    "problem,code",
    [
        ("unselected", "ASSET_NOT_USABLE"),
        ("draft_image", "ASSET_NOT_USABLE"),
        ("rejected_image", "ASSET_NOT_USABLE"),
        ("stale_image", "ASSET_NOT_USABLE"),
        ("archived_image", "ASSET_NOT_USABLE"),
        ("stale_timeline", "ASSET_NOT_USABLE"),
        ("archived_timeline", "ASSET_NOT_USABLE"),
        ("rejected_timeline", "ASSET_NOT_USABLE"),
        ("stale_board", "STORYBOARD_NOT_APPROVED"),
        ("script_registry", "ASSET_NOT_USABLE"),
        ("script_status", "RENDER_INPUT_CHANGED"),
        ("shot_status", "ASSET_NOT_USABLE"),
    ],
)
def test_current_lifecycle_selection_and_checks_are_revalidated(client, render_case, problem, code):
    value, _ = ready_manifest(client, render_case)
    clip = render_case["snapshot"]["tracks"][0]["clips"][0]
    with client.app.state.session_factory.begin() as db:
        image = db.get(AssetVersion, UUID(clip["source_asset_version_id"]))
        image_asset = db.get(VisualAsset, image.asset_id)
        timeline = db.get(AssetVersion, UUID(render_case["id"]))
        if problem == "unselected":
            image_asset.current_version_id = None
        elif problem in {"draft_image", "rejected_image"}:
            image.status = "DRAFT" if problem == "draft_image" else "REJECTED"
        elif problem == "stale_image":
            image.stale = True
        elif problem == "archived_image":
            image_asset.status = "ARCHIVED"
        elif problem == "stale_timeline":
            timeline.stale = True
        elif problem == "archived_timeline":
            db.get(VisualAsset, timeline.asset_id).status = "ARCHIVED"
        elif problem == "rejected_timeline":
            timeline.status = "REJECTED"
        elif problem == "stale_board":
            db.get(AssetVersion, UUID(render_case["snapshot"]["storyboard_id"])).stale = True
        elif problem in {"script_registry", "script_status"}:
            source = next(
                item for item in value["snapshot"]["sources"] if item["asset_type"] == "SCRIPT"
            )
            model = Script if problem == "script_status" else AssetVersion
            db.get(model, UUID(source["asset_version_id"])).status = "REJECTED"
        elif problem == "shot_status":
            db.get(Shot, UUID(clip["shot_id"])).status = "DRAFT"
    assert_blocked(client.post(render_path(value, "admission")), code)
    # History remains intact even when new work is blocked.
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value


def test_new_review_cannot_be_bypassed_by_old_pass_or_old_selected_version(
    client, render_case, timeline_case
):
    value, _ = ready_manifest(client, render_case)
    original = timeline_case[1][0]
    record, created = saved(client, original)
    assert not created
    rerun = with_issue(deepcopy(original))
    rerun.update(
        result_id=str(uuid4()),
        rerun={
            "previous_review_id": str(record.review.result_id),
            "previous_sha256": digest(record.model_dump(mode="json")),
            "reason": "导出前发现服装问题，重新审查",
        },
    )
    latest, _ = saved(client, rerun)
    assert_blocked(client.post(render_path(value, "admission")), "CONTINUITY_REVIEW_REQUIRED")
    admission_fixture(client, latest)
    assert_blocked(client.post(render_path(value, "admission")), "CONTINUITY_REVISION_REQUIRED")


@pytest.mark.parametrize("profile", ["PREVIEW", "FORMAL"])
@pytest.mark.parametrize("problem", ["missing", "replaced"])
def test_actual_objects_are_rechecked_even_after_approved_resume(
    client, render_case, profile, problem
):
    value, _ = ready_manifest(client, render_case, profile=profile)
    if profile == "FORMAL":
        row = request_export(client, value)
        decision = decide(client, row)
        assert decision.status_code == 200, decision.text
        assert resume(client, row, decision.json()).status_code == 200
    assert client.post(render_path(value, "admission")).status_code == 200
    source = next(item["file"] for item in value["snapshot"]["sources"] if item["file"])
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(source["file_id"]))
        store = ObjectStorage(client.app.state.settings)
        if problem == "missing":
            store.delete(file.object_key)
        else:
            store.path(file.object_key).write_bytes(b"changed after review")
    assert_blocked(client.post(render_path(value, "admission")), "OBJECT_UNAVAILABLE")


def test_historical_timeline_and_profile_remain_exact_after_new_version(client, render_case):
    value, _ = ready_manifest(client, render_case)
    edited(client, render_case, deepcopy(render_case["snapshot"]))
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(value["project_id"])).output_profile = {
            "width": 1920,
            "height": 1080,
            "fps": 60,
        }
    response = client.post(render_path(value, "admission"))
    assert response.status_code == 200, response.text
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value


def test_persisted_media_check_cannot_be_removed_after_capture(client, render_case):
    value, _ = ready_manifest(client, render_case)
    clip = render_case["snapshot"]["tracks"][0]["clips"][0]
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        image = db.get(AssetVersion, UUID(clip["source_asset_version_id"]))
        image.metadata_json = {**image.metadata_json, "media": {}}
    assert client.post(render_path(value, "admission")).status_code == 200
