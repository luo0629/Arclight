"""Single-line regeneration uses the existing TTS delivery and exact-version path."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_audio_production import snapshot
from test_audio_tts import create_ok, declaration, finish_audio, tts_case
from test_audio_tts_boundaries import (
    approve_changed_line,
    boundary_state,
    voice_version,
)
from test_generation_budget import budget, set_policy
from test_generation_delivery import deliver
from test_model_services import account
from test_native_media_integration import provider_for
from test_text_contract_v2 import save_script

from arclight.persistence.audio_production import AudioLineBinding
from arclight.persistence.audio_tts import AudioTTSRequest
from arclight.persistence.models import (
    AssetVersion,
    FileRegistration,
    GenerationJob,
    ProjectMember,
    VisualAsset,
)

__all__ = ["tts_case"]


def registered_audio(client, settings, case):
    row, job, audio = finish_audio(client, settings, case)
    endpoint = case["url"].removesuffix("/regenerate") + f"/{row['job_id']}"
    response = client.post(
        endpoint + "/result",
        json=declaration(expected_revision=case["payload"].get("expected_revision", 0)),
    )
    assert response.status_code == 201, response.text
    return response.json(), row, job, audio


def regeneration_case(case, source, **changes):
    return {
        **case,
        "url": case["url"].removesuffix("/regenerate") + "/regenerate",
        "payload": {
            "creation_key": str(uuid4()),
            "source_audio_version_id": source["binding"]["asset_version_id"],
            "voice_version_id": case["voice"]["id"],
            "expected_revision": source["asset_revision"],
            **changes,
        },
    }


def test_regeneration_appends_three_exact_versions_without_rebuilding_the_episode(
    client, settings, tts_case
):
    original, first, _, _ = registered_audio(client, settings, tts_case)
    second_case = regeneration_case(tts_case, original)
    second, second_row, _, _ = registered_audio(client, settings, second_case)
    third_case = regeneration_case(tts_case, second)
    third, third_row, _, _ = registered_audio(client, settings, third_case)
    identities = [
        original["binding"]["asset_version_id"],
        second["binding"]["asset_version_id"],
        third["binding"]["asset_version_id"],
    ]
    assert len(set(identities)) == 3
    assert second_row["source_audio_version_id"] == identities[0]
    assert third_row["source_audio_version_id"] == identities[1]
    assert first["source_audio_version_id"] is None
    assert second_row["target_asset_id"] == third_row["target_asset_id"] == first["target_asset_id"]
    root = tts_case["url"].removesuffix("/tts")
    bindings = client.get(root + "/bindings").json()
    assert [item["asset_version_id"] for item in bindings] == identities
    other_line = root.replace(tts_case["lines"][0]["line_id"], tts_case["lines"][1]["line_id"])
    assert client.get(other_line + "/bindings").json() == []
    assert (
        client.get(f"/api/v1/audio-snapshots/{tts_case['snapshot']['id']}/lines").json()
        == (tts_case["lines"])
    )
    with client.app.state.session_factory() as db:
        versions = list(
            db.scalars(
                select(AssetVersion)
                .where(AssetVersion.asset_id == UUID(first["target_asset_id"]))
                .order_by(AssetVersion.version_no)
            )
        )
        assert [row.version_no for row in versions] == [1, 2, 3]
        assert [str(row.id) for row in versions] == identities
        assert all(row.status == "DRAFT" and not row.stale for row in versions)
        assert db.get(VisualAsset, versions[0].asset_id).current_version_id is None
        assert db.scalar(select(func.count()).select_from(AudioTTSRequest)) == 3
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 3


def test_regeneration_replay_keeps_one_job_and_conflicts_on_changed_source_or_parameters(
    client, settings, tts_case
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    second, _, _, _ = registered_audio(client, settings, regeneration_case(tts_case, source))
    case = regeneration_case(tts_case, second)
    queued, _ = create_ok(client, case)
    before = boundary_state(client)
    replay = client.post(case["url"], json={**case["payload"], "expected_revision": 999})
    assert replay.status_code == 200, replay.text
    assert replay.json() == queued
    next_voice = voice_version(
        client,
        tts_case["pid"],
        tts_case["lines"][0]["speaker"],
        expected_version_no=1,
        parameters={"voice": "test-voice", "speed": 0.9},
    )
    for changes in (
        {"source_audio_version_id": source["binding"]["asset_version_id"]},
        {"voice_version_id": next_voice["id"]},
        {"performance_parameters": {"speed": 0.8}},
        {"scheduling_mode": "BATCH"},
        {"batch_id": str(uuid4())},
    ):
        response = client.post(case["url"], json={**case["payload"], **changes})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert boundary_state(client) == before


@pytest.mark.parametrize("change", ["target", "missing_revision", "missing_source", "bad_source"])
def test_regeneration_rejects_ambiguous_or_incomplete_commands_before_admission(
    client, settings, tts_case, change
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    case = regeneration_case(tts_case, source)
    payload = dict(case["payload"])
    if change == "target":
        payload["target_asset_id"] = str(uuid4())
    elif change == "missing_revision":
        del payload["expected_revision"]
    elif change == "missing_source":
        del payload["source_audio_version_id"]
    else:
        payload["source_audio_version_id"] = "not-a-version"
    before = boundary_state(client)
    response = client.post(case["url"], json=payload)
    assert response.status_code == 422, response.text
    assert boundary_state(client) == before


def test_regeneration_requires_current_asset_revision_without_leaving_partial_inputs(
    client, settings, tts_case
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    case = regeneration_case(tts_case, source, expected_revision=0)
    before = boundary_state(client)
    response = client.post(case["url"], json=case["payload"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"
    assert boundary_state(client) == before


@pytest.mark.parametrize("scope", ["missing", "other_line", "other_snapshot", "other_project"])
def test_regeneration_source_must_be_bound_to_the_exact_project_snapshot_and_line(
    client, settings, tts_case, scope
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    case = regeneration_case(tts_case, source)
    if scope == "missing":
        case["payload"]["source_audio_version_id"] = str(uuid4())
    elif scope == "other_line":
        line = tts_case["lines"][1]
        voice = voice_version(
            client,
            tts_case["pid"],
            line["speaker"],
            parameters={"voice": "test-voice", "speed": 1.0},
        )
        case["url"] = case["url"].replace(tts_case["lines"][0]["line_id"], line["line_id"])
        case["payload"]["voice_version_id"] = voice["id"]
    elif scope == "other_snapshot":
        captured = snapshot(client, tts_case["episode"], tts_case["script"])
        assert captured.status_code == 201, captured.text
        case["url"] = case["url"].replace(tts_case["snapshot"]["id"], captured.json()["id"])
    else:
        created = client.post("/api/v1/projects", json={"name": "不同项目"})
        assert created.status_code == 201, created.text
        episode = client.post(
            f"/api/v1/projects/{created.json()['id']}/episodes",
            json={"episode_no": 1, "title": "另一集", "target_duration_ms": 30000},
        ).json()
        script = save_script(client, episode)
        captured = snapshot(client, episode, script)
        assert captured.status_code == 201, captured.text
        line = client.get(f"/api/v1/audio-snapshots/{captured.json()['id']}/lines").json()[0]
        voice = voice_version(
            client,
            created.json()["id"],
            line["speaker"],
            parameters={"voice": "test-voice", "speed": 1.0},
        )
        case["url"] = (
            f"/api/v1/audio-snapshots/{captured.json()['id']}/lines/{line['line_id']}"
            "/tts/regenerate"
        )
        case["payload"]["voice_version_id"] = voice["id"]
    before = boundary_state(client)
    response = client.post(case["url"], json=case["payload"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AUDIO_REGENERATION_SOURCE_INVALID"
    assert boundary_state(client) == before


@pytest.mark.parametrize("role,status", [(None, 404), ("viewer", 403)])
@pytest.mark.parametrize("replay", [False, True])
def test_regeneration_rechecks_membership_for_new_commands_and_replays(
    client, settings, tts_case, role, status, replay
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    case = regeneration_case(tts_case, source)
    if replay:
        create_ok(client, case)
    member = account(client)
    if role:
        with client.app.state.session_factory.begin() as db:
            db.add(ProjectMember(project_id=UUID(tts_case["pid"]), user_id=UUID(member), role=role))
    before = boundary_state(client)
    response = client.post(case["url"], json=case["payload"])
    assert response.status_code == status, response.text
    assert boundary_state(client) == before


@pytest.mark.parametrize("status", ["ARCHIVED", "LOCKED", "STALE", "REJECTED"])
def test_regeneration_obeys_the_existing_audio_asset_lifecycle(client, settings, tts_case, status):
    source, row, _, _ = registered_audio(client, settings, tts_case)
    case = regeneration_case(tts_case, source)
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(row["target_asset_id"])).status = status
    before = boundary_state(client)
    response = client.post(case["url"], json=case["payload"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ASSET_READ_ONLY"
    assert boundary_state(client) == before


def test_changed_dialogue_blocks_new_regeneration_but_preserves_exact_history(
    client, settings, tts_case
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    root = tts_case["url"].removesuffix("/tts")
    history = client.get(root + "/history")
    assert history.status_code == 200, history.text
    approve_changed_line(client, tts_case)
    before = boundary_state(client)
    case = regeneration_case(tts_case, source)
    response = client.post(case["url"], json=case["payload"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AUDIO_LINE_NOT_USABLE"
    after = client.get(root + "/history")
    assert after.status_code == 200, after.text
    assert after.json()[0]["binding"] == history.json()[0]["binding"]
    assert after.json()[0]["version"]["stale"] is True
    assert boundary_state(client) == before


def test_successful_regeneration_result_cannot_bind_after_its_exact_dialogue_changes(
    client, settings, tts_case
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    case = regeneration_case(tts_case, source)
    row, _, _ = finish_audio(client, settings, case)
    approve_changed_line(client, tts_case)
    before = boundary_state(client)
    response = client.post(
        tts_case["url"] + f"/{row['job_id']}/result",
        json=declaration(expected_revision=source["asset_revision"]),
    )
    assert response.status_code == 409, response.text
    assert boundary_state(client) == before
    history = client.get(tts_case["url"].removesuffix("/tts") + "/history").json()
    assert len(history) == 1
    assert history[0]["binding"]["asset_version_id"] == source["binding"]["asset_version_id"]


def test_unrelated_dialogue_revision_does_not_prevent_regenerating_an_unchanged_line(
    client, settings, tts_case
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    content = deepcopy(tts_case["script"]["content_json"])
    content["scenes"][0]["dialogue"][1]["text"] = "只有另一句改变。"
    root = f"/api/v1/episodes/{tts_case['episode']['id']}/scripts"
    saved = client.post(
        root,
        json={
            "expected_version_no": 1,
            "base_version_id": tts_case["script"]["id"],
            "content": content,
        },
    )
    assert saved.status_code == 201, saved.text
    approved = client.post(root + f"/{saved.json()['id']}/review", json={"decision": "APPROVE"})
    assert approved.status_code == 200, approved.text
    regenerated, _, _, _ = registered_audio(client, settings, regeneration_case(tts_case, source))
    assert regenerated["binding"]["asset_version_id"] != source["binding"]["asset_version_id"]
    with client.app.state.session_factory() as db:
        assert not db.get(AssetVersion, UUID(source["binding"]["asset_version_id"])).stale


def test_regeneration_budget_rejection_has_no_task_manifest_or_version_side_effects(
    client, settings, tts_case
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    set_policy(client, tts_case["pid"], "0")
    case = regeneration_case(tts_case, source)
    before = boundary_state(client)
    response = client.post(case["url"], json=case["payload"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "BUDGET_EXCEEDED"
    assert boundary_state(client) == before


def test_unknown_regeneration_keeps_reservation_and_never_resubmits_on_replay(
    client, settings, tts_case
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    case = regeneration_case(tts_case, source)
    row, job = create_ok(client, case)
    calls = []

    def uncertain(request):
        calls.append(request)
        raise httpx.ReadTimeout("The provider may have accepted the request", request=request)

    provider = provider_for(client, settings, job, uncertain)
    assert deliver(client, job, provider) == "RESULT_UNKNOWN"
    before = boundary_state(client)
    budget_before = client.get(budget(tts_case["pid"])).json()
    assert Decimal(budget_before["pending_estimate"]) > 0
    replay = client.post(case["url"], json=case["payload"])
    assert replay.status_code == 200, replay.text
    assert replay.json()["job_id"] == row["job_id"]
    assert replay.json()["status"] == "RESULT_UNKNOWN"
    assert deliver(client, job, provider) == "RESULT_UNKNOWN"
    assert len(calls) == 1
    fresh = client.post(case["url"], json={**case["payload"], "creation_key": str(uuid4())})
    assert fresh.status_code == 409, fresh.text
    assert fresh.json()["error"]["code"] == "GENERATION_TARGET_BUSY"
    assert boundary_state(client) == before
    assert (
        client.get(budget(tts_case["pid"])).json()["pending_estimate"]
        == (budget_before["pending_estimate"])
    )
    endpoint = tts_case["url"] + f"/{row['job_id']}/result"
    assert client.post(endpoint, json=declaration(expected_revision=1)).status_code == 409
    assert len(client.get(tts_case["url"].removesuffix("/tts") + "/history").json()) == 1


@pytest.mark.parametrize("operation", ["create", "register"])
def test_postgres_concurrent_regeneration_replay_has_one_job_and_one_new_version(
    client, settings, tts_case, operation
):
    from arclight.application.audio_tts import regenerate_tts, register_result, require_tts
    from arclight.domain.audio_tts import AudioTTSRegenerate, AudioTTSRegister

    factory = client.app.state.session_factory
    with factory() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip("PostgreSQL single-line regeneration row locking required")
    source, _, _, _ = registered_audio(client, settings, tts_case)
    case = regeneration_case(tts_case, source)
    row = finish_audio(client, settings, case)[0] if operation == "register" else None
    actor = tts_case["voice"]["created_by"]
    snapshot_id = UUID(tts_case["snapshot"]["id"])
    line_id = tts_case["lines"][0]["line_id"]

    def execute(_):
        with factory.begin() as db:
            if operation == "create":
                result, created = regenerate_tts(
                    db, snapshot_id, line_id, actor, AudioTTSRegenerate(**case["payload"])
                )
                return str(result.job_id), created
            request = require_tts(db, snapshot_id, line_id, UUID(row["job_id"]), actor, write=True)
            result, created = register_result(
                db,
                settings,
                request,
                actor,
                AudioTTSRegister(**declaration(expected_revision=source["asset_revision"])),
            )
            return str(result.binding.asset_version_id), created

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(execute, range(2)))
    assert len({item[0] for item in receipts}) == 1
    assert sorted(item[1] for item in receipts) == [False, True]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AudioTTSRequest)) == 2
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 2
        expected = 2 if operation == "register" else 1
        assert db.scalar(select(func.count()).select_from(AudioLineBinding)) == expected
        assert db.scalar(select(func.count()).select_from(FileRegistration)) == expected
