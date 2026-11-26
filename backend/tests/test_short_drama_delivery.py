"""Verify the automatic finishing path against actual licensed inputs and rendering."""

import json
from datetime import timedelta
from io import BytesIO
from uuid import UUID, uuid4

import httpx
import pytest
from fontTools.ttLib import TTFont
from render_admission_helpers import assert_blocked
from render_manifest_helpers import review_case, stored_file, timeline_case
from sqlalchemy import func, select
from test_audio_production import output_audio, prepare, snapshot, wave_bytes
from test_collaboration import create_episode, create_project
from test_contexts import approved_board
from test_generation_delivery import deliver
from test_media_validation import font_bytes
from test_model_services import account
from test_native_media_integration import attempt_for, catalog_version, configure, provider_for
from test_text_contract_v2 import legacy_script, make_asset, save_script

from arclight.application.agent_runtime_state import initialize_run_state, scope_for_run
from arclight.application.generation_artifacts import import_attempt
from arclight.application.generation_state import transition
from arclight.application.production_versions import find_binding
from arclight.application.render_execution import execute_render
from arclight.application.render_inputs import bundled_font_receipt
from arclight.application.short_drama_audio import (
    DIALOGUE_PLAN,
    advance_short_drama_audio,
    dialogue_reconciliation,
    preflight_dialogue,
)
from arclight.application.short_drama_budget import bind_media_job
from arclight.application.short_drama_delivery import queue_short_drama_delivery
from arclight.application.short_drama_diagnostics import ShortDramaFailure, public_issue
from arclight.application.short_drama_visuals import bind_visual_rights
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_state import transition_agent_step
from arclight.domain.generation import GenerationStatus
from arclight.domain.producer_planning import WORKFLOW_VERSION
from arclight.domain.timelines import TimelineCreate
from arclight.domain.versions import digest
from arclight.media.bundled_font import BUNDLED_FONT_SHA256
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.audio_tts import AudioTTSRequest
from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.base import utcnow
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    GenerationAttempt,
    GenerationCandidate,
    GenerationJob,
    Project,
    Script,
    ServiceCredential,
    Shot,
    StoredFile,
    Storyboard,
    WorkflowRun,
)
from arclight.persistence.render_manifests import RenderManifest

__all__ = ["review_case", "timeline_case"]


def delivery_font(client, episode, script):
    factory = client.app.state.session_factory
    project_id = UUID(episode["project_id"])
    with factory() as db:
        row = db.get(Script, UUID(script["id"]))
        text = "".join(
            line["text"] for scene in row.content_json["scenes"] for line in scene["dialogue"]
        )
    font = TTFont(BytesIO(font_bytes()))
    for table in font["cmap"].tables:
        table.cmap.update({ord(char): "A" for char in text})
    data = BytesIO()
    font.save(data)
    font_id, _, _ = stored_file(client, project_id, data.getvalue(), "font/ttf")
    return font_id


def authorized_run(
    client,
    episode,
    storyboard_id,
    *,
    actor="service-owner",
    generated_rights=False,
    use_bundled_font=False,
    output_profile=None,
    agent_limits=None,
):
    factory = client.app.state.session_factory
    project_id = UUID(episode["project_id"])
    with factory.begin() as db:
        script = db.get(Script, db.get(Storyboard, storyboard_id).script_version_id)
        script_body = {"id": str(script.id)}
        script_digest = digest(script.content_json)
        output = output_profile or {"width": 256, "height": 256, "fps": 24}
        if use_bundled_font:
            font_id = bundled_font_receipt(db, client.app.state.settings, project_id).id
        db.get(Project, project_id).output_profile = output
    if not use_bundled_font:
        font_id = delivery_font(client, episode, script_body)
    spec = {
        "script_version_id": script_body["id"],
        "script_sha256": script_digest,
        "request_sha256": "a" * 64,
        "target_duration_ms": 30000,
        "output_profile": output,
        "font_file_id": str(font_id),
        "generated_media_rights_confirmed": generated_rights,
    }
    run_id = uuid4()
    with factory.begin() as db:
        run = WorkflowRun(
            id=run_id,
            episode_id=UUID(episode["id"]),
            workflow_type="producer",
            workflow_version=WORKFLOW_VERSION,
            status="RUNNING",
            trace_id=str(run_id),
            idempotency_key=str(run_id),
            request_fingerprint=digest(spec),
            input_json={"created_by": actor, "short_drama": spec},
        )
        db.add(run)
        db.flush()
        state = initialize_run_state(run, project_id)
        run.agent_state_json = transition_agent_step(
            state,
            scope_for_run(run, project_id),
            expected_revision=state.revision,
            step_id="producer",
            target="RUNNING",
        ).model_dump(mode="json")
        db.add(
            AuditEvent(
                project_id=project_id,
                actor_id=actor,
                resource_id=run_id,
                action="short_drama.authorized",
                details_json=spec,
            )
        )
        db.add(
            AgentRunLimit(
                run_id=run_id,
                project_id=project_id,
                root_run_id=run_id,
                actor_id=actor,
                input_sha256=state.input_sha256,
                depth=0,
                limits_json=(agent_limits or AgentRunLimits()).model_dump(mode="json"),
                deadline_at=utcnow() + timedelta(hours=1),
            )
        )
    return run_id, script_body


def approve_recordings(client, episode, script):
    created = snapshot(client, episode, script)
    assert created.status_code == 201, created.text
    snap = created.json()
    lines = client.get(f"/api/v1/audio-snapshots/{snap['id']}/lines").json()
    source = episode, script, snap, lines
    choices = {}
    for index, line in enumerate(lines):
        url, asset, manifest = prepare(client, source, index)
        identity = output_audio(client, source, asset, manifest)
        result = client.post(url + "/bindings", json={"asset_version_id": identity})
        assert result.status_code == 201, result.text
        state_url = (
            f"/api/v1/projects/{episode['project_id']}/assets/{asset}/versions/{identity}/state"
        )
        for revision, status in enumerate(("READY_FOR_REVIEW", "APPROVED")):
            result = client.patch(state_url, json={"expected_revision": revision, "status": status})
            assert result.status_code == 200, result.text
        choices[line["line_id"]] = UUID(identity)
    return choices


def bind_delivery_visuals(client, run_id, selected_shots):
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        for selected in selected_shots:
            version = db.get(AssetVersion, selected.asset_version_id)
            shot = db.get(Shot, selected.shot_id)
            candidate = db.scalar(
                select(GenerationCandidate).where(
                    GenerationCandidate.asset_version_id == version.id
                )
            )
            generation = db.get(GenerationJob, candidate.job_id)
            file = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
            assert not file.rights_confirmed
            bind_media_job(db, run.id, generation)
            bind_visual_rights(db, run, shot, candidate, version, file, purpose="FINAL_SHOT")


def test_draft_preflight_saves_voices_before_single_approval(client, settings):
    account(client)
    configure(client, settings, "image", "openai_image")
    project = create_project(client)
    episode = create_episode(client, project)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        referenced = make_asset(db, project["id"], "CHARACTER")
    content = legacy_script()
    content["characters"].append("访客")
    content["character_assets"] = [{"character": "访客", "asset": referenced}]
    content["scenes"][0]["dialogue"].append({"speaker": "访客", "text": "请进。"})
    script = save_script(client, episode, content, approve=False)
    path = f"/api/v1/episodes/{episode['id']}/short-drama"

    def readiness():
        response = client.get(path + "/preflight", params={"script_version_id": script["id"]})
        assert response.status_code == 200, response.text
        return response.json()

    def speech_issues(state):
        return [issue for issue in state["issues"] if issue["service_kind"] == "SPEECH"]

    assert speech_issues(readiness()) == [
        {
            "phase": "AUDIO",
            "code": "SHORT_DRAMA_SPEECH_SERVICE_REQUIRED",
            "service_kind": "SPEECH",
            "supplier": None,
            "model": None,
        }
    ]
    configure(client, settings, "speech", "openai_tts")
    initial = readiness()
    assert initial["blocked_voices"] == []
    assert speech_issues(initial) == []
    assert "project_budget_limit" not in initial
    assert initial["needed_voices"] == [
        {"speaker": "主角", "character_asset_id": None},
        {"speaker": "旁白", "character_asset_id": None},
        {"speaker": "访客", "character_asset_id": referenced["asset_id"]},
    ]

    def save_voices(expected_version_no):
        voices = {}
        for choice in initial["needed_voices"]:
            saved = client.post(
                f"/api/v1/projects/{project['id']}/audio-voices",
                json={
                    **choice,
                    "creation_key": str(uuid4()),
                    "expected_version_no": expected_version_no,
                    "model_config_version": catalog_version(client, project["id"]),
                    "parameters": {"voice": "test-voice"},
                },
            )
            assert saved.status_code == 201, saved.text
            voices[choice["speaker"]] = saved.json()["id"]
        return voices

    original = save_voices(0)
    eligible = readiness()
    assert eligible["needed_voices"] == eligible["blocked_voices"] == []
    assert speech_issues(eligible) == []
    with factory() as db:
        assert db.get(Script, UUID(script["id"])).status == "READY_FOR_REVIEW"
        assert db.scalar(select(func.count()).select_from(HumanReviewRequest)) == 1
        for model in (AudioSnapshot, GenerationJob, GatewayCall, WorkflowRun, HumanReviewDecision):
            assert db.scalar(select(func.count()).select_from(model)) == 0

    previous_prices = settings.service_model_policies
    settings.service_model_policies = []
    unquoted = readiness()
    assert unquoted["needed_voices"] == unquoted["blocked_voices"] == []
    settings.service_model_policies = [
        row.model_copy(update={"pricing_version": "test-native:2"}) for row in previous_prices
    ]
    changed = readiness()
    assert changed["needed_voices"] == changed["blocked_voices"] == []
    settings.service_model_policies = previous_prices

    configure(client, settings, "speech", "openai_tts", revision=1, key="changed-key")
    with factory.begin() as db:
        voice = db.get(AudioVoiceVersion, UUID(original["主角"]))
        credential = db.get(ServiceCredential, UUID(voice.service_snapshot_json["credential_id"]))
        credential.encrypted_key = "unreadable-synthetic-credential"
    unavailable = readiness()
    assert unavailable["needed_voices"] == initial["needed_voices"]
    assert {row["reason"] for row in unavailable["blocked_voices"]} == {
        "已保存音色的语音服务凭据不可用。"
    }
    assert speech_issues(unavailable) == [
        {
            "phase": "AUDIO",
            "code": "SERVICE_KEY_UNAVAILABLE",
            "service_kind": "SPEECH",
            "supplier": "custom",
            "model": "native-model",
        }
    ]
    assert "unreadable-synthetic-credential" not in json.dumps(unavailable)
    voices = save_voices(1)
    recovered = readiness()
    assert recovered["needed_voices"] == recovered["blocked_voices"] == []

    font_id = delivery_font(client, episode, script)
    queued = client.post(
        path,
        json={
            "script_version_id": script["id"],
            "generated_media_rights_confirmed": True,
            "font_file_id": str(font_id),
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert queued.status_code == 202, queued.text
    run_id = UUID(queued.json()["run_id"])
    with factory() as db:
        assert db.get(Script, UUID(script["id"])).status == "APPROVED"
        assert db.scalar(select(func.count()).select_from(HumanReviewRequest)) == 1
        assert db.scalar(select(func.count()).select_from(HumanReviewDecision)) == 1
        bound = find_binding(db, UUID(project["id"]), DIALOGUE_PLAN, run_id)
        assert bound.snapshot_json["voice_asset_versions"] == {}
        for line in script["content_json"]["scenes"][0]["dialogue"]:
            assert bound.snapshot_json["voices"][line["line_id"]] == {
                "voice_version_id": voices[line["speaker"]],
                "voice_profile_version_id": None,
            }
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.scalar(select(func.count()).select_from(GatewayCall)) == 0


def test_preflight_flags_recording_without_usage_rights(client, episode):
    script = save_script(client, episode)
    created = snapshot(client, episode, script)
    assert created.status_code == 201, created.text
    snap = created.json()
    lines = client.get(f"/api/v1/audio-snapshots/{snap['id']}/lines").json()
    source = episode, script, snap, lines
    _, asset_id, manifest_id = prepare(client, source)
    version_id = UUID(output_audio(client, source, asset_id, manifest_id, rights=False))
    with client.app.state.session_factory.begin() as db:
        recording_file = db.scalar(
            select(StoredFile).where(StoredFile.asset_version_id == version_id)
        )
        db.add(
            AudioLineBinding(
                snapshot_id=UUID(snap["id"]),
                line_id=lines[0]["line_id"],
                asset_version_id=version_id,
                dependency_manifest_id=UUID(manifest_id),
                file_id=recording_file.id,
                duration_ms=100,
                created_by="service-owner",
            )
        )
        db.get(AssetVersion, version_id).status = "APPROVED"
    preflight = client.get(
        f"/api/v1/episodes/{episode['id']}/short-drama/preflight",
        params={"script_version_id": script["id"]},
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["needed_voices"] == [{"speaker": "旁白", "character_asset_id": None}]
    assert preflight.json()["blocked_voices"] == [
        {"speaker": "主角", "reason": "已批准录音的文件回执或使用授权不可用。"}
    ]


def test_automatic_formal_delivery_uses_exact_assets_and_single_owner_grant(
    client, settings, episode, timeline_case
):
    body, _ = timeline_case
    request = TimelineCreate.model_validate(body)
    run_id, script = authorized_run(
        client, episode, request.storyboard_id, generated_rights=True, use_bundled_font=True
    )
    factory = client.app.state.session_factory
    with pytest.raises(DomainError) as missing, factory.begin() as db:
        preflight_dialogue(
            db,
            settings,
            db.get(WorkflowRun, run_id),
            UUID(episode["project_id"]),
            "service-owner",
            UUID(script["id"]),
        )
    assert missing.value.code == "SHORT_DRAMA_SPEECH_SERVICE_REQUIRED"

    voices = approve_recordings(client, episode, script)
    path = f"/api/v1/episodes/{episode['id']}/short-drama/preflight"
    preflight = client.get(path, params={"script_version_id": script["id"]})
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["needed_voices"] == preflight.json()["blocked_voices"] == []

    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        preflight_dialogue(
            db, settings, run, UUID(episode["project_id"]), "service-owner", UUID(script["id"])
        )
        sound = advance_short_drama_audio(db, settings, run, request.storyboard_id)
        assert sound is not None and sound.voice_asset_versions == voices
        assert sound.sound_clips == ()

    def queue():
        with factory.begin() as db:
            return queue_short_drama_delivery(
                db,
                settings,
                db.get(WorkflowRun, run_id),
                request.storyboard_id,
                request.selected_shots,
                voice_asset_versions=voices,
            )

    with pytest.raises(DomainError) as unlicensed:
        queue()
    assert unlicensed.value.code == "SHORT_DRAMA_VISUAL_RIGHTS"
    bind_delivery_visuals(client, run_id, request.selected_shots)
    job = queue()
    assert job.profile == "FORMAL" and job.status == "QUEUED"
    assert queue() == job
    with factory() as db:
        value = db.get(RenderManifest, job.manifest_id).snapshot_json
        picture, voice, _, _, subtitle = value["timeline"]["tracks"]
        assert value["font"]["sha256"] == BUNDLED_FONT_SHA256
        assert (
            value["font"]["file_id"]
            == db.get(WorkflowRun, run_id).input_json["short_drama"]["font_file_id"]
        )
        assert value["timeline"]["duration_ms"] == 30000
        assert [clip["duration_ms"] for clip in picture["clips"]] == [6000] * 5
        assert {clip["source_asset_version_id"] for clip in voice["clips"]} == {
            str(identity) for identity in voices.values()
        }
        assert len(subtitle["clips"]) == len(voices)
        assert (
            db.scalar(
                select(func.count(HumanReviewRequest.id)).where(
                    HumanReviewRequest.handler == "RENDER_EXPORT"
                )
            )
            == 0
        )
    assert_blocked(
        client.post(
            f"/api/v1/render-manifests/{job.manifest_id}/formal-export",
            json={"action": "RETRY", "retry_of": str(job.id)},
        ),
        "SHORT_DRAMA_EXPORT_SCOPE",
    )
    assert execute_render(factory, settings, job.id) == "SUCCEEDED"
    final = client.get(f"/api/v1/render-jobs/{job.id}").json()
    assert final["is_delivery"] is True and final["output_file_id"] and final["report_id"]

    with factory.begin() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.resource_id == run_id, AuditEvent.action == "short_drama.authorized"
            )
        )
        db.delete(event)
    assert_blocked(
        client.post(f"/api/v1/render-manifests/{job.manifest_id}/admission"),
        "SHORT_DRAMA_GRANT_REQUIRED",
    )


@pytest.mark.parametrize("outcome", ["success", "authentication", "unknown", "queued_credential"])
def test_automatic_dialogue_uses_exact_audio_and_reports_failures(client, settings, outcome):
    actor = account(client)
    configure(client, settings, "speech", "openai_tts")
    project = create_project(client)
    episode = create_episode(client, project)
    script = save_script(client, episode)
    board, _ = approved_board(client, episode, script)
    board_id = UUID(board["id"])
    lines = {line["line_id"]: line for line in script["content_json"]["scenes"][0]["dialogue"]}
    original_voices = {}
    for speaker in {line["speaker"] for line in lines.values()}:
        voice = client.post(
            f"/api/v1/projects/{project['id']}/audio-voices",
            json={
                "creation_key": str(uuid4()),
                "speaker": speaker,
                "expected_version_no": 0,
                "model_config_version": catalog_version(client, project["id"]),
                "parameters": {"voice": "test-voice", "speed": 1.0},
                "output_spec": {"format": "wav"},
            },
        )
        assert voice.status_code == 201, voice.text
        original_voices[speaker] = UUID(voice.json()["id"])
    run_id, _ = authorized_run(client, episode, board_id, actor=actor, generated_rights=True)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        preflight_dialogue(
            db,
            settings,
            db.get(WorkflowRun, run_id),
            UUID(project["id"]),
            actor,
            UUID(script["id"]),
        )
    configure(client, settings, "speech", "openai_tts", revision=1, key="changed-key")
    for speaker in original_voices:
        saved = client.post(
            f"/api/v1/projects/{project['id']}/audio-voices",
            json={
                "creation_key": str(uuid4()),
                "speaker": speaker,
                "expected_version_no": 1,
                "model_config_version": catalog_version(client, project["id"]),
                "parameters": {"voice": "new-voice"},
            },
        )
        assert saved.status_code == 201, saved.text

    def advance():
        with factory.begin() as db:
            return advance_short_drama_audio(db, settings, db.get(WorkflowRun, run_id), board_id)

    assert advance() is None
    assert advance() is None
    with factory() as db:
        requests = {}
        for request in db.scalars(select(AudioTTSRequest)):
            assert request.voice_version_id == original_voices[lines[request.line_id]["speaker"]]
            requests[request.line_id] = request.job_id
    assert set(requests) == set(lines)
    if outcome != "success":
        job = {"id": str(next(iter(requests.values())))}
        if outcome == "queued_credential":
            with factory.begin() as db:
                voice = db.get(AudioVoiceVersion, next(iter(original_voices.values())))
                credential = db.get(
                    ServiceCredential, UUID(voice.service_snapshot_json["credential_id"])
                )
                credential.encrypted_key = "unreadable-queued-credential"
            with pytest.raises(ShortDramaFailure) as failed:
                advance()
            with factory() as db:
                assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 0
            issue = failed.value.record("AUDIO")
            assert issue["code"] == "SERVICE_KEY_UNAVAILABLE"
        elif outcome == "authentication":
            provider = provider_for(
                client,
                settings,
                job,
                lambda _: httpx.Response(
                    401, json={"error": {"message": "synthetic private upstream data"}}
                ),
            )
            assert deliver(client, job, provider) == "FAILED"
            with pytest.raises(ShortDramaFailure) as failed:
                advance()
            issue = failed.value.record("AUDIO")
            assert issue["code"] == "GATEWAY_PROVIDER_AUTHENTICATION_FAILED"
            assert "synthetic private upstream data" not in json.dumps(issue)
        else:

            def timeout(request):
                raise httpx.ReadTimeout("synthetic response loss", request=request)

            assert (
                deliver(client, job, provider_for(client, settings, job, timeout))
                == "RESULT_UNKNOWN"
            )
            assert advance() is None
            with factory() as db:
                pending = dialogue_reconciliation(db, UUID(project["id"]), run_id)
            assert pending is not None
            issue = pending.record("AUDIO")
            assert issue["code"] == "SHORT_DRAMA_TTS_UNCERTAIN"
            assert public_issue(issue, actor).task.id == UUID(job["id"])
            assert public_issue(issue, "another-account").task is None
            with factory.begin() as db:
                row = db.get(GenerationJob, UUID(job["id"]))
                transition(db, row.id, GenerationStatus.RECONCILING, row.revision)
                transition(db, row.id, GenerationStatus.MANUAL_REVIEW, row.revision)
            assert advance() is None
            with factory() as db:
                manual = dialogue_reconciliation(db, UUID(project["id"]), run_id)
            assert manual is not None and manual.record("AUDIO") == issue
        assert public_issue(issue, actor).model == "native-model"
        assert public_issue(issue, "another-account").model is None
        return

    for line_id, identity in requests.items():
        job = {"id": str(identity)}

        def respond(request, expected_text=lines[line_id]["text"]):
            assert json.loads(request.content)["input"] == expected_text
            assert request.headers["Authorization"] == "Bearer first-key"
            return httpx.Response(200, content=wave_bytes(), headers={"content-type": "audio/wav"})

        provider = provider_for(client, settings, job, respond)
        assert deliver(client, job, provider) == "SUCCEEDED"
        assert advance() is None
        assert import_attempt(factory, settings, attempt_for(client, job), provider) == 1

    sound = advance()
    assert sound is not None and set(sound.voice_asset_versions) == set(lines)
    assert advance() == sound
    with factory() as db:
        for identity in sound.voice_asset_versions.values():
            version = db.get(AssetVersion, identity)
            assert version.status == "APPROVED"
            raw = db.get(StoredFile, UUID(version.metadata_json["audio_tts"]["source_file_id"]))
            assert raw.rights_confirmed is False
            result = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == identity))
            assert result.rights_confirmed and str(run_id) in result.rights_note
