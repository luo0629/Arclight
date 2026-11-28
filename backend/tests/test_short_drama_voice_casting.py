import json
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
from speech_catalog_support import json_catalog
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_native_media_integration import configure
from test_short_drama_delivery import delivery_font
from test_short_drama_production import configured_episode as configured_episode
from test_short_drama_production import create_short_drama
from test_text_contract_v2 import save_script

from arclight.application.gateway_accounting import reconcile
from arclight.application.producer_execution import execute_producer
from arclight.application.producer_interaction import (
    command_receipt,
    command_rows,
    enqueue_short_drama_voice_assignment,
    record_receipt,
)
from arclight.application.production_versions import find_binding
from arclight.application.short_drama_audio import DIALOGUE_PLAN
from arclight.domain.model_gateway import GatewayUsage
from arclight.domain.producer_interaction import VOICE_CASTING_ACTION
from arclight.domain.versions import SHORT_DRAMA_VOICE_CATALOG
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import Episode, WorkflowRun


def test_voice_casting_reconciles_original_call_and_rejects_unknown_voice(
    client, settings, configured_episode, model_service, monkeypatch
):
    episode = configured_episode
    project_id = UUID(episode["project_id"])
    configure(client, settings, "speech", "openai_tts")

    def discover(request):
        assert request.url.params["model"] == "native-model"
        return json_catalog("native-model", "test-voice")

    monkeypatch.setattr(
        "arclight.providers.speech_catalogs.PublicHTTPTransport",
        lambda _timeout, **_kwargs: httpx.MockTransport(discover),
    )
    script = save_script(client, episode)
    font_id = delivery_font(client, episode, script)
    queued = create_short_drama(client, episode, script, font_file_id=str(font_id))
    assert queued.status_code == 202, queued.text
    run_id = UUID(queued.json()["run_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        actor = run.input_json["created_by"]
        (plan,) = command_rows(db, run, project_id)
        record_receipt(db, run, project_id, plan, "COMMITTED")
        enqueue_short_drama_voice_assignment(db, run, db.get(Episode, run.episode_id), actor)
        casting = command_rows(db, run, project_id)[-1]
        assert casting.snapshot_json["action"] == VOICE_CASTING_ACTION
        command_id, revision = casting.resource_id, casting.snapshot_json["revision"]

    sent = []

    def provider(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            raise httpx.ReadTimeout("synthetic timeout before a trusted response")
        seed = json.loads(payload["messages"][1]["content"])["data"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "choices": [
                                        {"role_id": role["role_id"], "voice_id": "made-up-voice"}
                                        for role in seed["roles"]
                                    ]
                                }
                            ),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "actual_cost": "0"},
            },
        )

    model_service(provider)
    assert execute_producer(factory, settings, run_id, revision)
    assert execute_producer(factory, settings, run_id, revision)
    assert len(sent) == 1
    with factory.begin() as db:
        assert command_receipt(db, project_id, command_id) is None
        assert db.scalar(select(AgentLoop).where(AgentLoop.run_id == run_id)).status == (
            "RECOVERY_REQUIRED"
        )
        catalog = find_binding(db, project_id, SHORT_DRAMA_VOICE_CATALOG, command_id)
        assert catalog is not None
        assert catalog.snapshot_json["scope"]["run_id"] == str(run_id)
        assert catalog.snapshot_json["catalog"]["options"][0]["id"] == "test-voice"
        call = db.scalar(select(GatewayCall).where(GatewayCall.workflow_id == run_id))
        reconcile(
            db,
            call,
            actor,
            "NOT_ACCEPTED",
            GatewayUsage(actual_cost=Decimal("0")),
            "synthetic fixture: provider confirms no acceptance",
        )

    def unexpected_query(_source):
        raise AssertionError("Recovery must reuse the original command's frozen catalog")

    monkeypatch.setattr(
        "arclight.application.short_drama_voice_casting.query_voice_catalog", unexpected_query
    )
    recover = client.post(
        f"/api/v1/runs/{run_id}/producer/commands",
        headers={"Idempotency-Key": uuid4().hex},
        json={"action": "RECOVER", "expected_revision": revision, "command_id": str(command_id)},
    )
    assert recover.status_code == 202, recover.text
    assert execute_producer(factory, settings, run_id, revision)
    assert execute_producer(factory, settings, run_id, revision)
    assert len(sent) == 2 and sent[0] == sent[1]
    assert not sent[-1].get("tools")
    seed = json.loads(sent[-1]["messages"][1]["content"])["data"]
    assert [voice["id"] for voice in seed["voices"]] == ["test-voice"]
    with factory() as db:
        receipt = command_receipt(db, project_id, command_id)
        assert receipt["status"] == "STOPPED"
        assert receipt["error_code"] == "SHORT_DRAMA_VOICE_SELECTION_INVALID"
        assert receipt["issue_detail"] == {"service_kind": "TEXT"}
        loop = db.scalar(select(AgentLoop).where(AgentLoop.run_id == run_id))
        actions = list(
            db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id))
        )
        calls = list(db.scalars(select(GatewayCall).where(GatewayCall.workflow_id == run_id)))
        assert len(actions) == 1 and len(calls) == 2
        assert find_binding(db, project_id, DIALOGUE_PLAN, run_id) is None
        voices = select(AudioVoiceVersion.id).where(AudioVoiceVersion.project_id == project_id)
        assert db.scalar(voices) is None
    bindings = client.get(
        f"/api/v1/projects/{project_id}/production-versions/bindings",
        params={"resource_id": str(command_id)},
    )
    assert bindings.status_code == 200, bindings.text
    assert "test-voice" not in bindings.text and "made-up-voice" not in bindings.text
    assert SHORT_DRAMA_VOICE_CATALOG not in {row["resource_type"] for row in bindings.json()}


def test_short_drama_discovers_voice_directory_before_admission(
    client, settings, configured_episode, monkeypatch
):
    episode = configured_episode
    configure(client, settings, "speech", "openai_tts")
    script = save_script(client, episode)
    font_id = delivery_font(client, episode, script)
    keys = set()
    available = False

    def discover(request):
        assert request.url.path == "/v1/audio/voice-catalog"
        keys.add(request.headers["Authorization"])
        if not available:
            return httpx.Response(404)
        return json_catalog(request.url.params["model"], "available-voice")

    monkeypatch.setattr(
        "arclight.providers.speech_catalogs.PublicHTTPTransport",
        lambda _timeout, **_kwargs: httpx.MockTransport(discover),
    )
    readiness = f"/api/v1/episodes/{episode['id']}/short-drama/preflight"
    parameters = {"script_version_id": script["id"]}
    missing = client.get(readiness, params=parameters).json()
    assert missing["issues"][0]["code"] == "AUDIO_VOICE_CATALOG_UNAVAILABLE"
    key = uuid4().hex
    rejected = create_short_drama(client, episode, script, key=key, font_file_id=str(font_id))
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "AUDIO_VOICE_CATALOG_UNAVAILABLE"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(WorkflowRun).where(WorkflowRun.idempotency_key == key)) is None

    available = True
    ready = client.get(readiness, params=parameters).json()
    assert ready["issues"] == []
    started = create_short_drama(client, episode, script, key=key, font_file_id=str(font_id))
    assert started.status_code == 202, started.text
    assert keys == {"Bearer first-key"}

    available = False
    replayed = create_short_drama(client, episode, script, key=key, font_file_id=str(font_id))
    assert replayed.status_code == 202 and replayed.json()["run_id"] == started.json()["run_id"]
