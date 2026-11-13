"""Subtitle provenance extends exact timeline versions and the existing dependency graph."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import audio_case, render_case, review_case, timeline_case
from sqlalchemy import func, select
from test_concurrency import require_postgres
from test_dependency_invalidation import apply, produce
from test_dependency_validation import frozen_manifest, registry_version
from test_subtitle_lifecycle import approve_subtitle, asset_url
from test_subtitle_production import (
    counts,
    generate,
    save_timeline,
    subtitle_case,
    subtitle_source,
)

from arclight.application.dependencies import attach_output, input_usable
from arclight.application.subtitles import generate_subtitles
from arclight.core.errors import DomainError
from arclight.domain.dependencies import validate_dependency_type
from arclight.domain.invalidation import affects_input
from arclight.domain.subtitles import SubtitleGenerate
from arclight.persistence.models import AssetDependency, AssetVersion, VisualAsset

__all__ = [
    "audio_case",
    "render_case",
    "review_case",
    "subtitle_case",
    "subtitle_source",
    "timeline_case",
]


@pytest.mark.parametrize(
    "source,target,edge,allowed",
    [
        ("TIMELINE", "SUBTITLE", "SEQUENCE_DEPENDENCY", True),
        ("STORYBOARD", "SUBTITLE", "SEQUENCE_DEPENDENCY", False),
        ("AUDIO", "SUBTITLE", "SEQUENCE_DEPENDENCY", False),
        ("SUBTITLE", "SUBTITLE", "SEQUENCE_DEPENDENCY", False),
        ("TIMELINE", "AUDIO", "SEQUENCE_DEPENDENCY", False),
        ("TIMELINE", "SUBTITLE", "RENDER_DEPENDENCY", False),
    ],
)
def test_subtitle_sequence_contract_only_admits_the_exact_timeline_pair(
    source, target, edge, allowed
):
    if allowed:
        validate_dependency_type(source, target, edge)
    else:
        with pytest.raises(DomainError) as error:
            validate_dependency_type(source, target, edge)
        assert error.value.code == "INVALID_DEPENDENCY_TYPE"


def test_only_a_real_sealed_timeline_can_supply_draft_sequence_input(client, episode, render_case):
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(render_case["id"]))
        asset = db.get(VisualAsset, version.asset_id)
        assert version.status == "DRAFT"
        assert input_usable(db, version, asset, "SUBTITLE")
        assert input_usable(db, version, asset, "RENDER")
        for target in ("AUDIO", "SHOT_IMAGE", "TIMELINE"):
            assert not input_usable(db, version, asset, target)
        for kind in ("TIMELINE", "AUDIO", "SUBTITLE"):
            fake_asset, fake = registry_version(
                db,
                UUID(episode["project_id"]),
                kind=kind,
                status="DRAFT",
                manifest_id=UUID(version.metadata_json["dependency_manifest_id"]),
            )
            assert not input_usable(db, fake, fake_asset, "SUBTITLE")
        version.stale = True
        assert not input_usable(db, version, asset, "SUBTITLE")
        version.stale = False
        asset.status = "ARCHIVED"
        assert not input_usable(db, version, asset, "SUBTITLE")


@pytest.mark.parametrize("source_kind", ["TIMELINE", "STORYBOARD", "AUDIO", "SUBTITLE"])
@pytest.mark.parametrize("edge", ["SEQUENCE_DEPENDENCY", "CONTENT_DEPENDENCY"])
def test_sequence_invalidation_adds_subtitles_only_for_timeline_sequence_inputs(source_kind, edge):
    identity = str(uuid4())
    change = {"source_asset_version_id": identity, "change_kind": "SEQUENCE", "selectors": []}
    item = {
        "source_asset_version_id": identity,
        "asset_type": source_kind,
        "dependency_type": edge,
        "selectors": [],
    }
    assert affects_input(change, item, "SUBTITLE") == (
        source_kind == "TIMELINE" and edge == "SEQUENCE_DEPENDENCY"
    )


def test_timeline_subtitle_next_timeline_is_version_acyclic_and_sequence_change_propagates(
    client, episode
):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        old_asset, old = registry_version(db, pid, kind="TIMELINE", manifest_id=uuid4())
        subtitle = produce(db, pid, "SUBTITLE", (old_asset, old), "SEQUENCE_DEPENDENCY")
        fresh = AssetVersion(
            asset_id=old_asset.id,
            version_no=2,
            checksum="c" * 64,
            object_key="test-only:timeline-v2",
            created_by="service-owner",
            status="APPROVED",
            metadata_json={"dependency_manifest_id": str(uuid4())},
        )
        db.add(fresh)
        db.flush()
        manifest = frozen_manifest(db, pid, old_asset, fresh, [subtitle], kind="RENDER_DEPENDENCY")
        attach_output(db, pid, manifest.id, fresh)
        assert db.scalar(select(func.count()).select_from(AssetDependency)) == 2
        cycle = frozen_manifest(db, pid, old_asset, old, [subtitle], kind="RENDER_DEPENDENCY")
        with pytest.raises(DomainError) as error:
            attach_output(db, pid, cycle.id, old)
        assert error.value.code == "DEPENDENCY_CYCLE"
        assert db.scalar(select(func.count()).select_from(AssetDependency)) == 2
        event, _ = apply(db, pid, old, "SEQUENCE")
        assert {row["asset_version_id"] for row in event.snapshot_json["affected"]} == {
            str(subtitle[1].id),
            str(fresh.id),
        }
        assert subtitle[1].stale and fresh.stale
        assert not old.stale


def subtitle_snapshot(case):
    snapshot = deepcopy(case["timeline"]["snapshot"])
    track = next(track for track in snapshot["tracks"] if track["kind"] == "SUBTITLE")
    track["clips"] = deepcopy(case["track"]["clips"])
    return snapshot


def test_generated_subtitle_clips_append_to_the_existing_timeline_without_rewriting_sources(
    client, subtitle_case
):
    case = subtitle_case
    approve_subtitle(client, case)
    original = deepcopy(case["timeline"])
    result = save_timeline(client, original, subtitle_snapshot(case))
    clips = next(
        track["clips"] for track in result["snapshot"]["tracks"] if track["kind"] == "SUBTITLE"
    )
    assert [{key: value for key, value in clip.items() if key != "id"} for clip in clips] == [
        {key: value for key, value in clip.items() if key != "id"}
        for clip in case["track"]["clips"]
    ]
    assert {clip["source_asset_version_id"] for clip in clips} == {case["track"]["id"]}
    assert client.get(f"/api/v1/timelines/{original['id']}").json() == original
    with client.app.state.session_factory() as db:
        edges = list(db.scalars(select(AssetDependency)))
        pairs = {
            (str(edge.source_asset_version_id), str(edge.target_asset_version_id)) for edge in edges
        }
        assert (original["id"], case["track"]["id"]) in pairs
        assert (case["track"]["id"], result["id"]) in pairs


@pytest.mark.parametrize(
    "field", ["start_ms", "duration_ms", "source_start_ms", "audio_version_id", "line_id"]
)
def test_generated_subtitle_clips_reject_changed_frozen_cue_declarations_atomically(
    client, subtitle_case, field
):
    case = subtitle_case
    approve_subtitle(client, case)
    snapshot = subtitle_snapshot(case)
    clip = next(track for track in snapshot["tracks"] if track["kind"] == "SUBTITLE")["clips"][0]
    if field == "audio_version_id":
        clip[field] = case["audio"][1]["id"]
    elif field == "line_id":
        clip[field] = case["lines"][1]["line_id"]
    else:
        clip[field] += 1
    before = counts(client)
    response = client.patch(
        f"/api/v1/timelines/{case['timeline']['id']}",
        json={
            "creation_key": str(uuid4()),
            "expected_version_no": case["timeline"]["version_no"],
            "snapshot": snapshot,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "SUBTITLE_CLIP_MISMATCH"
    assert counts(client) == before
    assert client.get(f"/api/v1/timelines/{case['timeline']['id']}").json() == case["timeline"]


def test_preproduction_schema1_subtitle_clips_remain_consumable_in_the_existing_save_path(
    client, audio_case
):
    timeline, _, _ = audio_case
    assert timeline["snapshot"]["schema_version"] == 1
    result = save_timeline(client, timeline, deepcopy(timeline["snapshot"]))
    assert next(
        track["clips"] for track in result["snapshot"]["tracks"] if track["kind"] == "SUBTITLE"
    )
    assert client.get(f"/api/v1/timelines/{timeline['id']}").json() == timeline


@pytest.mark.parametrize("operation", ["replay", "cas"])
def test_postgres_subtitle_generation_serializes_replay_and_target_revision(
    client, settings, subtitle_source, operation
):
    require_postgres(settings)
    case = subtitle_source
    if operation == "cas":
        response = generate(client, case)
        assert response.status_code == 201, response.text
        case = {**case, "track": response.json()}
        asset = client.get(asset_url(case)).json()
        payload = {
            **case["payload"],
            "target_asset_id": case["track"]["asset_id"],
            "expected_revision": asset["revision"],
        }
    else:
        payload = case["payload"]
    before = counts(client)
    barrier = Barrier(2)
    factory = client.app.state.session_factory

    def execute(index):
        command = {**payload}
        if operation == "cas":
            command["creation_key"] = str(uuid4())
        barrier.wait(timeout=15)
        try:
            with factory.begin() as db:
                result = generate_subtitles(
                    db,
                    settings,
                    UUID(case["episode"]["id"]),
                    SubtitleGenerate.model_validate(command),
                    case["audio"][0]["created_by"],
                )
                return "created", str(result.id)
        except DomainError as error:
            return "conflict", error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    if operation == "replay":
        assert len({identity for status, identity in results if status == "created"}) == 1
        assert all(status == "created" for status, _ in results)
        expected = (1, 1, 1, 1, 1)
    else:
        assert sorted(status for status, _ in results) == ["conflict", "created"]
        assert [identity for status, identity in results if status == "conflict"] == [
            "VERSION_CONFLICT"
        ]
        expected = (0, 1, 1, 1, 1)
    assert (
        tuple(after - initial for initial, after in zip(before, counts(client), strict=True))
        == expected
    )


@pytest.mark.parametrize("change_kind", ["SEQUENCE", "CONTENT", "DIALOGUE"])
def test_actual_consumed_sequence_audio_and_dialogue_invalidate_only_related_subtitle_versions(
    client, subtitle_case, change_kind
):
    case = subtitle_case
    snapshot = deepcopy(case["timeline"]["snapshot"])
    voice = next(track for track in snapshot["tracks"] if track["kind"] == "VOICE")
    voice["clips"] = [voice["clips"][1]]
    voice["clips"][0]["ordinal"] = 0
    independent_timeline = save_timeline(client, case["timeline"], snapshot)
    response = generate(
        client, case, creation_key=str(uuid4()), timeline_id=independent_timeline["id"]
    )
    assert response.status_code == 201, response.text
    independent = response.json()
    with client.app.state.session_factory.begin() as db:
        if change_kind == "SEQUENCE":
            identity = case["timeline"]["id"]
        elif change_kind == "CONTENT":
            identity = case["audio"][0]["id"]
        else:
            identity = case["script"]["id"]
        source = db.get(AssetVersion, UUID(identity))
        selectors = [f"dialogue:{case['lines'][0]['line_id']}"] if change_kind == "DIALOGUE" else []
        event, _ = apply(db, UUID(case["episode"]["project_id"]), source, change_kind, selectors)
        affected = {row["asset_version_id"] for row in event.snapshot_json["affected"]}
        assert case["track"]["id"] in affected
        assert independent["id"] not in affected
        assert db.get(AssetVersion, UUID(case["track"]["id"])).stale
        assert not db.get(AssetVersion, UUID(independent["id"])).stale
    history = client.get(f"/api/v1/subtitle-tracks/{case['track']['id']}")
    assert history.status_code == 200, history.text
    assert history.json()["cues"] == case["track"]["cues"]
