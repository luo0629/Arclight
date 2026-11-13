"""Music uses the existing typed DAG and exact-version invalidation contracts."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import manifest
from sqlalchemy import select
from test_continuity_reviews import review_case
from test_dependency_invalidation import produce
from test_music_assets import import_music, music_source, music_url
from test_music_timeline_integration import selected_music_case
from test_timeline_saves import save_url, saved_case, saved_timeline
from test_timelines import timeline_case

from arclight.application.invalidation import invalidate
from arclight.core.errors import DomainError
from arclight.domain.dependencies import validate_dependency_type
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    DependencyManifest,
    DependencyOutput,
    VisualAsset,
)
from arclight.persistence.timelines import Timeline

__all__ = ["review_case", "saved_case", "timeline_case"]


@pytest.mark.parametrize("target", ["TIMELINE", "RENDER"])
def test_music_allows_only_existing_render_dependency_targets(target):
    validate_dependency_type("MUSIC", target, "RENDER_DEPENDENCY")


@pytest.mark.parametrize(
    "target", ["SCRIPT", "STORYBOARD", "AUDIO", "SUBTITLE", "CHARACTER", "SCENE", "MUSIC"]
)
def test_music_render_dependency_cannot_broaden_target_types(target):
    with pytest.raises(DomainError) as error:
        validate_dependency_type("MUSIC", target, "RENDER_DEPENDENCY")
    assert error.value.code == "INVALID_DEPENDENCY_TYPE"


@pytest.mark.parametrize("target", ["TIMELINE", "RENDER"])
@pytest.mark.parametrize(
    "kind",
    ["CONTENT_DEPENDENCY", "STYLE_DEPENDENCY", "REFERENCE_DEPENDENCY", "SEQUENCE_DEPENDENCY"],
)
def test_music_cannot_substitute_an_unrelated_dependency_type(target, kind):
    with pytest.raises(DomainError) as error:
        validate_dependency_type("MUSIC", target, kind)
    assert error.value.code == "INVALID_DEPENDENCY_TYPE"


@pytest.mark.parametrize("role", ["BACKGROUND", "AMBIENCE", "TRANSITION"])
def test_music_exact_consumed_version_invalidates_only_its_real_dependency_closure(
    client, episode, saved_case, role
):
    original, body, asset, version, _, _ = selected_music_case(client, episode, saved_case, role)
    timeline = saved_timeline(client, original, body)
    frozen_render, _ = manifest(client, timeline)
    project_id = UUID(episode["project_id"])
    source_id, timeline_id = UUID(version["id"]), UUID(timeline["id"])
    with client.app.state.session_factory.begin() as db:
        timeline_version = db.get(AssetVersion, timeline_id)
        timeline_asset = db.get(VisualAsset, timeline_version.asset_id)
        dependency = db.get(DependencyManifest, UUID(timeline["dependency_manifest_id"]))
        frozen_dependency = deepcopy(dependency.snapshot_json)
        music_input = next(
            item
            for item in frozen_dependency["inputs"]
            if item["source_asset_version_id"] == version["id"]
        )
        assert music_input["asset_type"] == "MUSIC"
        assert music_input["checksum"] == version["checksum"]
        assert music_input["dependency_type"] == "RENDER_DEPENDENCY"
        assert (
            db.scalar(
                select(DependencyOutput.id).where(
                    DependencyOutput.dependency_manifest_id == dependency.id,
                    DependencyOutput.asset_version_id == timeline_id,
                )
            )
            is not None
        )
        assert (
            db.scalar(
                select(AssetDependency.id).where(
                    AssetDependency.source_asset_version_id == source_id,
                    AssetDependency.target_asset_version_id == timeline_id,
                    AssetDependency.dependency_type == "RENDER_DEPENDENCY",
                )
            )
            is not None
        )
        # A deterministic DAG fixture tests propagation; it does not claim an encoded MP4.
        _, render_version = produce(
            db, project_id, "RENDER", (timeline_asset, timeline_version), "RENDER_DEPENDENCY"
        )
        render_id = render_version.id
        actor = db.get(Timeline, timeline_id).created_by

    asset = client.get(music_url(asset)).json()
    unused = import_music(client, asset, music_source(client, episode), role)
    assert client.get(music_url(asset)).json()["current_version_id"] == version["id"]
    assert client.get(save_url(timeline)).json() == timeline
    with client.app.state.session_factory.begin() as db:
        unrelated, _ = invalidate(
            db,
            project_id,
            actor,
            uuid4(),
            [
                {
                    "source_asset_version_id": unused["id"],
                    "expected_revision": unused["revision"],
                    "change_kind": "RENDER",
                    "selectors": [],
                }
            ],
            "An unconsumed exact music version changed",
        )
        assert unrelated.snapshot_json["affected"] == []
        assert not db.get(AssetVersion, timeline_id).stale
        assert not db.get(AssetVersion, render_id).stale
        changed, _ = invalidate(
            db,
            project_id,
            actor,
            uuid4(),
            [
                {
                    "source_asset_version_id": version["id"],
                    "expected_revision": version["revision"],
                    "change_kind": "RENDER",
                    "selectors": [],
                }
            ],
            "The exact music consumed by this timeline changed",
        )
        assert {item["asset_version_id"] for item in changed.snapshot_json["affected"]} == {
            str(timeline_id),
            str(render_id),
        }
        assert db.get(AssetVersion, timeline_id).stale
        assert db.get(AssetVersion, render_id).stale
        assert not db.get(AssetVersion, UUID(original["id"])).stale
        assert (
            db.get(DependencyManifest, UUID(timeline["dependency_manifest_id"])).snapshot_json
            == frozen_dependency
        )
    historical = client.get(save_url(timeline)).json()
    assert historical["stale"] is True
    assert historical["snapshot"] == timeline["snapshot"]
    assert historical["content_sha256"] == timeline["content_sha256"]
    assert client.get(f"/api/v1/render-manifests/{frozen_render['id']}").json() == frozen_render
