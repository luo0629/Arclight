"""Cross-episode voices, immutable delivery and selector-scoped replacement."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from render_manifest_helpers import stored_file
from test_asset_versions import image_bytes
from test_audio_production import snapshot, wave_bytes
from test_audio_tts import create_ok, declaration, finish_audio
from test_audio_version_lifecycle import (
    approve_audio,
    audio_urls,
    read_ok,
    select_audio,
    selection_payload,
)
from test_generation_delivery import deliver, event_for
from test_model_services import account
from test_native_media_integration import configure, provider_for
from test_script_state_commit import capture, review, save, screenplay
from test_series_planning import planned_outline as planned_outline
from test_story_ledger import approve_ledger, save_ledger
from test_text_contract_v2 import make_asset, save_board
from test_writing_context import writing_case as writing_case

from arclight.agents.contracts import legacy_script_view, read_script_content
from arclight.application.audio_production import validate_audio_line
from arclight.application.dependencies import attach_output, capture_manifest
from arclight.application.timeline_store import append_timeline
from arclight.core.errors import DomainError
from arclight.domain.dependencies import DependencyInput, ManifestCreate
from arclight.domain.timelines import TRACK_KINDS, ClipSnapshot, TimelineSnapshot, TrackSnapshot
from arclight.persistence.generation import GenerationDispatch
from arclight.persistence.models import (
    AssetVersion,
    DependencyInvalidation,
    DependencyManifest,
    Episode,
    VisualAsset,
)
from arclight.providers.text import MockTextProvider


def post(client, url, body, status=201):
    response = client.post(url, json=body)
    assert response.status_code == status, response.text
    return response.json()


def voice(client, pid, character, speaker, name, expected):
    return post(
        client,
        f"/api/v1/projects/{pid}/audio-voices",
        {
            "creation_key": str(uuid4()),
            "speaker": speaker,
            "character_asset_id": character["asset_id"],
            "expected_version_no": expected,
            "parameters": {"voice": name, "speed": 1.0},
            "output_spec": {"format": "wav"},
        },
    )


def record_sources(client, episode, script):
    result = snapshot(client, episode, script)
    assert result.status_code == 201, result.text
    snap = result.json()
    return {
        "episode": episode,
        "pid": episode["project_id"],
        "script": script,
        "snapshot": snap,
        "lines": read_ok(client, f"/api/v1/audio-snapshots/{snap['id']}/lines"),
    }


def line_url(source, index):
    snapshot_id = source["snapshot"]["id"]
    line_id = source["lines"][index]["line_id"]
    return f"/api/v1/audio-snapshots/{snapshot_id}/lines/{line_id}"


def tts_case_for(source, index, profile, voice_row, **changes):
    return {
        **source,
        "url": line_url(source, index) + "/tts",
        "lines": [source["lines"][index]],
        "payload": {
            "creation_key": str(uuid4()),
            "voice_version_id": voice_row["id"],
            **({"voice_profile_version_id": profile["id"]} if profile else {}),
            **changes,
        },
    }


def scene_mapping(episode, script, scene, time):
    return {
        "id": str(uuid4()),
        "episode_id": episode["id"],
        "episode_no": episode["episode_no"],
        "script_version_id": script["id"],
        "scene_id": scene["scene_id"],
        "scene_no": scene["scene_no"],
        "segment_no": 1,
        "story_time": time,
    }


def ledger_head(client, pid):
    return read_ok(client, f"/api/v1/projects/{pid}/story-ledger")


def ledger(client, episode, mappings):
    token = str(uuid4())
    head = ledger_head(client, episode["project_id"])
    created = save_ledger(
        client,
        episode,
        {"schema_version": "1.0", "scene_times": mappings, "entries": []},
        revision=head["revision"],
        base_version_id=head["latest_version_id"],
        key=token,
    )
    return approve_ledger(client, episode, created, key=token + "-approve")


def profile_payload(ledger_row, normal, changed):
    return {
        "expected_version_no": 0,
        "creation_key": str(uuid4()),
        "ledger_version_id": ledger_row["head"]["approved_version_id"],
        "default_voice_version_id": normal["id"],
        "changes": [
            {
                "story_start": 10,
                "story_end": 20,
                "voice_version_id": changed["id"],
                "reason": "回忆中含着药，声音发哑",
            }
        ],
    }


def version_profile_url(pid, character):
    return f"/api/v1/projects/{pid}/characters/{character['asset_id']}/voice-profile"


@pytest.fixture
def series_voice_case(client, settings, request):
    # Establish the same real account for writing, story approval and speech configuration.
    owner = account(client)
    configure(client, settings, "speech", "openai_tts")
    case = request.getfixturevalue("writing_case")
    first = case["episode"]
    pid = first["project_id"]
    second = post(
        client,
        f"/api/v1/projects/{pid}/episodes",
        {"episode_no": 2, "title": "再遇", "target_duration_ms": 30000},
    )
    with client.app.state.session_factory.begin() as db:
        character = make_asset(db, pid, "CHARACTER")
    normal = voice(client, pid, character, "阿青", "test-voice", 0)
    changed = voice(client, pid, character, "阿青", "hoarse-test-voice", 1)
    mappings = []
    sources = []
    for i, episode in enumerate((first, second)):
        anchor = 40 if i == 0 else 20
        writing = {**case, "episode": episode}
        head = ledger_head(client, pid)
        context = capture(
            client,
            writing,
            target_story_time=anchor,
            expected_ledger_head_revision=head["revision"],
            ledger_version_id=head["approved_version_id"],
        )
        content = screenplay(writing, context)
        content["character_assets"] = [{"character": "阿青", "asset": character}]
        scene = content["scenes"][0]
        scene["state_changes"] = []
        if i == 0:
            memory = deepcopy(scene)
            memory.update(scene_id=str(uuid4()), scene_no=2, title="旧日渡口")
            memory["dramatic_beat"]["story_time"] = 10
            memory["dialogue"][0].update(line_id=str(uuid4()), emotion="含着药低声说")
            memory["dramatic_beat"]["dialogue_interactions"][0].update(
                line_ids=[memory["dialogue"][0]["line_id"]], action="先喘息，再示意同行"
            )
            content["scenes"].append(memory)
        else:
            scene["dialogue"][0]["emotion"] = "松了口气"
        script = save(client, writing, content)
        approved = review(client, writing, script)
        assert approved.status_code == 200, approved.text
        sources.append(record_sources(client, episode, script))
        mappings.extend(
            scene_mapping(episode, script, scene, scene["dramatic_beat"]["story_time"])
            for scene in script["content_json"]["scenes"]
        )
    ledger_row = ledger(client, first, mappings)
    url = version_profile_url(pid, character)
    return {
        "owner": owner,
        "pid": pid,
        "character": character,
        "sources": sources,
        "ledger": ledger_row,
        "mappings": mappings,
        "normal": normal,
        "changed": changed,
        "url": url,
        "payload": profile_payload(ledger_row, normal, changed),
    }


def test_profile_resolves_cross_episode_flashback_and_frozen_delivery(
    client, settings, series_voice_case
):
    case = series_voice_case
    first, second = case["sources"]
    profile = post(client, case["url"], case["payload"])
    assert post(client, case["url"], case["payload"], 200) == profile
    conflict = post(client, case["url"], {**case["payload"], "creation_key": str(uuid4())}, 409)
    assert conflict["error"]["code"] == "AUDIO_VOICE_PROFILE_CONFLICT"
    for source, index, time, chosen, emotion in (
        (first, 0, 40, case["normal"], "谨慎而坚定"),
        (first, 1, 10, case["changed"], "含着药低声说"),
        (second, 0, 20, case["normal"], "松了口气"),
    ):
        entry = read_ok(client, line_url(source, index) + "/voice-profile")
        selection = entry["selection"]
        assert entry["voice"]["id"] == chosen["id"]
        assert selection["profile_version_id"] == profile["id"]
        assert selection["mapping_id"] in {mapping["id"] for mapping in case["mappings"]}
        assert selection["story_time"] == time
        assert selection["performance"]["emotion"] == emotion
        assert selection["performance"]["purpose"] == "护送红信"
        assert bool(selection["change_reason"]) == (time == 10)
        job_case = tts_case_for(
            source, index, profile, chosen, performance_parameters={"speed": 0.8}
        )

        requests = []
        row, _, _ = finish_audio(client, settings, job_case, handler=audio_capture(requests))
        (body,) = requests
        assert body["voice"] == chosen["parameters"]["voice"]
        assert body["input"] == source["lines"][index]["text"]
        assert body["speed"] == 0.8
        assert emotion in body["instructions"]
        assert selection["performance"]["interactions"][0] in body["instructions"]
        with client.app.state.session_factory() as db:
            generation = db.get(DependencyManifest, UUID(row["generation_manifest_id"]))
            assert generation.snapshot_json["inputs"][1]["source_asset_version_id"] == profile["id"]
            frozen = generation.snapshot_json["context"]["audio_line"]
            manifest = db.get(DependencyManifest, UUID(frozen["dependency_manifest_id"]))
            assert manifest.snapshot_json["context"]["voice_selection"] == selection


def make_dependent(db, pid, actor, kind, sources):
    asset = VisualAsset(asset_type=kind, project_id=UUID(pid), name=kind, created_by=actor)
    db.add(asset)
    db.flush()
    manifest, _ = capture_manifest(
        db,
        UUID(pid),
        actor,
        ManifestCreate(
            creation_key=uuid4(),
            target_asset_id=asset.id,
            inputs=[
                DependencyInput(source_asset_version_id=UUID(identity), dependency_type=edge)
                for identity, edge in sources
            ],
        ),
    )
    version = AssetVersion(
        asset_id=asset.id,
        version_no=1,
        object_key=f"tests/voice-profile/{asset.id}",
        checksum="a" * 64,
        created_by=actor,
        status="APPROVED",
        metadata_json={"dependency_manifest_id": str(manifest.id)},
    )
    db.add(version)
    db.flush()
    attach_output(db, UUID(pid), manifest.id, version)
    return str(version.id)


def audio_capture(requests):
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=wave_bytes(), headers={"content-type": "audio/wav"})

    return respond


def complete_recording(client, settings, job_case):
    row, job, _ = finish_audio(client, settings, job_case, handler=audio_capture([]))
    result_url = job_case["url"] + f"/{job['id']}/result"
    receipt = post(client, result_url, declaration())
    asset_url, url = audio_urls(
        job_case, row["target_asset_id"], receipt["binding"]["asset_version_id"]
    )
    return approve_audio(client, url), asset_url, url, receipt


def test_profile_change_invalidates_exact_consumers_and_keeps_locked_history(
    client, settings, series_voice_case
):
    case = series_voice_case
    first, second = case["sources"]
    profile = post(client, case["url"], case["payload"])
    source = tts_case_for(first, 0, profile, case["normal"])
    keep = tts_case_for(second, 0, profile, case["normal"])
    original, asset_url, original_url, receipt = complete_recording(client, settings, source)
    independent, _, independent_url, _ = complete_recording(client, settings, keep)
    selected = select_audio(client, asset_url, original_url)
    locked = post(client, original_url + "/lock", selection_payload(selected), 200)
    assert locked["status"] == "LOCKED"
    later_snapshot = record_sources(client, first["episode"], first["script"])
    queued_source = tts_case_for(later_snapshot, 0, profile, case["normal"])
    _, queued = create_ok(client, queued_source)
    late_snapshot = record_sources(client, first["episode"], first["script"])
    late_source = tts_case_for(late_snapshot, 0, profile, case["normal"])
    _, late, _ = finish_audio(client, settings, late_source, handler=audio_capture([]))
    with client.app.state.session_factory.begin() as db:
        upstream = [(original["id"], "CONTENT_DEPENDENCY")]
        subtitle = make_dependent(db, case["pid"], case["owner"], "SUBTITLE", upstream)
        video = make_dependent(db, case["pid"], case["owner"], "VIDEO", upstream)
        timeline = make_dependent(
            db,
            case["pid"],
            case["owner"],
            "TIMELINE",
            [(video, "RENDER_DEPENDENCY"), (subtitle, "RENDER_DEPENDENCY")],
        )
        render = make_dependent(
            db, case["pid"], case["owner"], "RENDER", [(timeline, "SEQUENCE_DEPENDENCY")]
        )
    new_payload = deepcopy(case["payload"])
    new_payload.update(creation_key=str(uuid4()), expected_version_no=1)
    new_payload["changes"].append({**new_payload["changes"][0], "story_start": 40, "story_end": 41})
    updated = post(client, case["url"], new_payload)
    assert read_ok(client, case["url"] + f"?version_id={profile['id']}") == profile
    history = read_ok(client, line_url(first, 0) + "/history")
    assert history[0]["binding"] == receipt["binding"]
    assert history[0]["version"]["status"] == "LOCKED"
    assert history[0]["version"]["stale"] is True
    assert read_ok(client, independent_url) == independent
    for source_case, recording, stale in (
        (first, original, True),
        (second, independent, False),
    ):
        evidence = read_ok(
            client, f"/api/v1/episodes/{source_case['episode']['id']}/series-evidence"
        )
        assert evidence["bound_audio_total"] == 1
        (item,) = evidence["bound_audio"]
        assert item["version_id"] == recording["id"]
        assert item["status"] == ("LOCKED" if stale else "APPROVED")
        assert item["stale"] is stale
        assert item["voice"] == {
            "version_id": case["normal"]["id"],
            "version_no": case["normal"]["version_no"],
            "speaker": case["normal"]["speaker"],
            "profile_version_id": profile["id"],
        }
    with client.app.state.session_factory() as db:
        invalidation = db.get(DependencyInvalidation, UUID(updated["invalidation_id"]))
        assert {item["asset_version_id"] for item in invalidation.snapshot_json["affected"]} == {
            original["id"],
            subtitle,
            video,
            timeline,
            render,
        }
    provider = provider_for(
        client, settings, queued, lambda _: pytest.fail("stale job was dispatched")
    )
    assert deliver(client, queued, provider) == "FAILED"
    late_result = post(client, late_source["url"] + f"/{late['id']}/result", declaration(), 409)
    assert late_result["error"]["code"] == "AUDIO_VOICE_PROFILE_CHANGED"
    with client.app.state.session_factory() as db:
        event = db.get(GenerationDispatch, event_for(client, queued))
        assert event.receipt_json["error_code"] == "AUDIO_VOICE_PROFILE_CHANGED"
    stale_request = {**source["payload"], "creation_key": str(uuid4())}
    rejected = post(client, source["url"], stale_request, 409)
    assert rejected["error"]["code"] == "AUDIO_VOICE_PROFILE_CONFLICT"
    replacement = tts_case_for(first, 0, updated, case["changed"])
    created, _ = create_ok(client, replacement)
    assert created["target_asset_id"] != original["asset_id"]
    assert read_ok(client, asset_url) == locked


def test_dialogue_revision_reuses_only_the_unchanged_approved_recording(
    client, settings, series_voice_case
):
    case = series_voice_case
    first, _ = case["sources"]
    profile = post(client, case["url"], case["payload"])
    changed, _, changed_url, _ = complete_recording(
        client, settings, tts_case_for(first, 0, profile, case["normal"])
    )
    keep = tts_case_for(first, 1, profile, case["changed"])
    kept, _, kept_url, original = complete_recording(client, settings, keep)
    initial = first["script"]
    revised = deepcopy(initial["content_json"])
    context = revised["narrative_context"]
    head = ledger_head(client, case["pid"])
    writing = {
        "episode": first["episode"],
        "request": {
            key: context[key]
            for key in (
                "series_plan_version_id",
                "target_scene_no",
                "target_story_time",
                "character_ids",
            )
        },
    }
    revised["narrative_context"] = capture(
        client,
        writing,
        ledger_version_id=head["approved_version_id"],
        expected_ledger_head_revision=head["revision"],
    )
    revised["scenes"][0]["dialogue"][0]["text"] = "留在岸边，我去请人帮忙。"
    script = save(client, writing, revised, expected=1, base=initial["id"])
    approved = review(client, writing, script)
    assert approved.status_code == 200, approved.text
    current = record_sources(client, first["episode"], script)
    assert read_ok(client, changed_url)["stale"] is True
    assert read_ok(client, kept_url) == kept
    assert read_ok(client, line_url(current, 0) + "/history?include_reusable=true") == []
    assert read_ok(client, line_url(current, 1) + "/history") == []
    reused = read_ok(client, line_url(current, 1) + "/history?include_reusable=true")
    assert len(reused) == 1
    assert reused[0]["binding"] == original["binding"]
    assert reused[0]["binding"]["snapshot_id"] == first["snapshot"]["id"]
    track = subtitles_for_reuse(client, case, current, kept)
    (cue,) = track["cues"]
    assert cue["script_version_id"] == script["id"]
    assert cue["script_sha256"] == current["snapshot"]["script_sha256"]
    assert cue["audio_binding_id"] == original["binding"]["id"]
    assert cue["audio_snapshot_id"] == original["binding"]["snapshot_id"]
    assert read_ok(client, f"/api/v1/subtitle-tracks/{track['id']}")["cues"] == track["cues"]
    with client.app.state.session_factory() as db:
        validate_audio_line(db, UUID(kept["id"]), UUID(script["id"]), first["lines"][1]["line_id"])
        with pytest.raises(DomainError, match="精确剧本台词"):
            validate_audio_line(
                db, UUID(changed["id"]), UUID(script["id"]), first["lines"][0]["line_id"]
            )


def subtitles_for_reuse(client, case, current, recording):
    episode = current["episode"]
    script = current["script"]
    line = current["lines"][1]
    content = legacy_script_view(read_script_content(script["content_json"]))
    board_content = MockTextProvider().storyboard(content, 30000).model_dump(mode="json")
    response = save_board(client, episode, script, board_content)
    assert response.status_code == 201, response.text
    board = response.json()
    prefix = f"/api/v1/episodes/{episode['id']}"
    post(client, prefix + f"/storyboards/{board['id']}/review", {"decision": "APPROVE"}, 200)
    shot = read_ok(client, prefix + f"/shots?storyboard_id={board['id']}")[0]
    _, still, checksum = stored_file(
        client, case["pid"], image_bytes(), "image/png", kind="SHOT_IMAGE"
    )
    video = ClipSnapshot(
        id=uuid4(),
        ordinal=0,
        source_asset_version_id=still,
        source_sha256=checksum,
        start_ms=0,
        duration_ms=500,
        shot_id=UUID(shot["id"]),
    )
    voice_clip = ClipSnapshot(
        id=uuid4(),
        ordinal=0,
        source_asset_version_id=UUID(recording["id"]),
        source_sha256=recording["checksum"],
        start_ms=200,
        duration_ms=100,
        script_version_id=UUID(script["id"]),
        line_id=line["line_id"],
    )
    # Persist a timeline fixture through its real store. Visual selection is covered
    # by render tests and plays no part in reading this voice clip or its subtitle.
    with client.app.state.session_factory.begin() as db:
        episode_row = db.get(Episode, UUID(episode["id"]))
        snapshot = TimelineSnapshot(
            episode_id=episode_row.id,
            storyboard_id=UUID(board["id"]),
            duration_ms=500,
            default_duration_ms=500,
            output_profile={},
            tracks=[
                TrackSnapshot(
                    id=uuid4(),
                    kind=kind,
                    ordinal=index,
                    clips={"VIDEO": [video], "VOICE": [voice_clip]}.get(kind, []),
                )
                for index, kind in enumerate(TRACK_KINDS)
            ],
        )
        timeline = append_timeline(db, episode_row, case["owner"], uuid4(), "a" * 64, snapshot)
    return post(
        client,
        prefix + "/subtitle-tracks",
        {
            "creation_key": str(uuid4()),
            "timeline_id": str(timeline.id),
        },
    )


def test_first_profile_blocks_conflicting_queued_legacy_voice(client, settings, series_voice_case):
    case = series_voice_case
    first, _ = case["sources"]
    legacy = tts_case_for(first, 1, None, case["normal"])
    _, job = create_ok(client, legacy)
    profile = post(client, case["url"], case["payload"])
    # A retry of the original submission returns the original immutable job.
    assert post(client, legacy["url"], legacy["payload"], 200)["job_id"] == job["id"]
    provider = provider_for(client, settings, job, lambda _: pytest.fail("legacy voice was sent"))
    assert deliver(client, job, provider) == "FAILED"
    assert read_ok(client, line_url(first, 1) + "/voice-profile")["profile"] == profile


def test_profile_requires_project_membership_exact_voice_and_disjoint_override(
    client, series_voice_case
):
    case = series_voice_case
    payload = deepcopy(case["payload"])
    payload["changes"].append({**payload["changes"][0], "story_start": 19, "story_end": 21})
    post(client, case["url"], payload, 422)
    with client.app.state.session_factory.begin() as db:
        other = make_asset(db, case["pid"], "CHARACTER")
    wrong_role = voice(client, case["pid"], other, "阿青", "wrong-role", 2)
    post(
        client, case["url"], {**case["payload"], "default_voice_version_id": wrong_role["id"]}, 422
    )
    other_speaker = voice(client, case["pid"], case["character"], "石岚", "wrong-speaker", 0)
    profile = post(
        client, case["url"], {**case["payload"], "default_voice_version_id": other_speaker["id"]}
    )
    first, _ = case["sources"]
    selection = read_ok(client, line_url(first, 0) + "/voice-profile")
    assert selection["selection"] is None and "说话者" in selection["unavailable_reason"]
    request = tts_case_for(first, 0, profile, other_speaker)
    result = post(client, request["url"], request["payload"], 422)
    assert result["error"]["code"] == "AUDIO_VOICE_SPEAKER_MISMATCH"
    assert len(read_ok(client, request["url"])) == 0
    registered = account(client)
    assert registered != case["owner"]
    assert client.get(case["url"]).status_code == 404
    post(client, case["url"], case["payload"], 404)
