"""DeepSeek workflow contract; paid acceptance is an explicit opt-in."""

import json
import os
from pathlib import Path
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from test_api_workflow import advance, review, start
from test_model_gateway import api
from text_service_support import use_test_text_service

from arclight.agents.contracts import ScriptContent
from arclight.application.runtime import execute_run
from arclight.domain.model_gateway import GatewayFailure, GatewayPolicy
from arclight.domain.model_suppliers import supplier_by_id
from arclight.domain.versions import digest
from arclight.main import create_app
from arclight.persistence.models import Review, Script, Storyboard, WorkflowRun
from arclight.providers.network import PublicHTTPTransport
from arclight.providers.text import MockTextProvider, OpenAICompatibleTextProvider


def test_authorized_deepseek_text_workflow(
    client, episode, settings, monkeypatch, tmp_path, stub_llm
):
    live = os.getenv("ARCLIGHT_LIVE_TEXT_APPROVAL") == "deepseek-synthetic-story"
    if live:
        assert settings.database_url.startswith("sqlite:///"), (
            "Use isolated SQLite for live acceptance"
        )
        key = (
            Path(os.environ["ARCLIGHT_LIVE_TEXT_KEY_FILE"]).read_text(encoding="utf-8-sig").strip()
        )
        assert key, "The explicitly supplied local DeepSeek key file is empty"
        model = os.environ["ARCLIGHT_LIVE_TEXT_MODEL_ID"]
    else:
        key, model = "synthetic-deepseek-test-key", "exact-deepseek-fixture"
    supplier = supplier_by_id("deepseek")
    settings.provider = "openai_compatible"
    settings.llm_base_url = supplier.base_url
    settings.llm_model = model
    settings.llm_supplier = supplier.id
    settings.llm_response_format = supplier.response_format
    settings.llm_api_key = SecretStr(key)
    use_test_text_service(settings, monkeypatch)
    settings.llm_timeout_seconds = 180
    settings.model_gateway_policy = GatewayPolicy(timeout_seconds=180)
    story = (
        "虚构短故事：雨停后，小岚在公园长椅上捡到一把蓝色雨伞。"
        "她正准备离开，阿树跑来寻找爷爷留下的伞。小岚询问伞柄上的标记，"
        "阿树说是一颗刻歪的小星星。确认后，小岚把伞交还，两人相视一笑。"
        "制作30秒的一场戏，只有这两名虚构人物和少量对白。"
    )
    evidence_dir = (
        Path(os.getenv("ARCLIGHT_LIVE_TEXT_EVIDENCE_DIR", str(tmp_path))) if live else tmp_path
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_file = evidence_dir / "deepseek-text-evidence.json"
    assert not evidence_file.exists(), "Review the existing live evidence before another paid run"
    evidence = {
        "mode": "authorized-live" if live else "offline-fixture",
        "provider": supplier.id,
        "model": settings.llm_model,
        "story": story,
        "story_sha256": digest(story),
        "application_cost_cap": None,
        "database_url": settings.database_url,
        "checkpoint_url": settings.checkpoint_url,
        "failure_injection": "local quota rejection before storyboard provider submission",
        "states": [],
        "provider_responses": [],
    }

    def save_evidence():
        evidence_file.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2).replace(key, "[REDACTED]"),
            encoding="utf-8",
        )

    def retain(state):
        evidence["states"].append(state)
        evidence["calls"] = client.get(api(episode) + "/calls").json()
        save_evidence()

    class RecordingTransport(httpx.BaseTransport):
        def __init__(self, *args, **kwargs):
            self.inner = PublicHTTPTransport(*args, **kwargs)

        def handle_request(self, request):
            response = self.inner.handle_request(request)
            response.read()
            try:
                body = response.json()
            except ValueError:
                body = {"invalid_json": True}
            evidence["provider_responses"].append({"status": response.status_code, "body": body})
            save_evidence()
            return response

        def close(self):
            self.inner.close()

    def synthetic_response(request):
        wire = json.loads(request.content)
        assert wire["model"] == model and request.headers["authorization"] == f"Bearer {key}"
        assert wire["response_format"] == {"type": "json_object"}
        assert wire["thinking"] == {"type": "disabled"}
        data = json.loads(wire["messages"][-1]["content"])["task_input"]
        duration = data["target_duration_ms"]
        mock = MockTextProvider()
        if "script" in data:
            result = mock.storyboard(ScriptContent.model_validate(data["script"]), duration)
        else:
            result = mock.script(data["story"], data["style_guide"], duration)
        body = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": result.model_dump_json()},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
        evidence["provider_responses"].append({"status": 200, "body": body})
        return httpx.Response(200, json=body)

    if live:
        monkeypatch.setattr(
            "arclight.providers.llm_transport.PublicHTTPTransport", RecordingTransport
        )
    else:
        stub_llm(synthetic_response)

    created = start(client, episode, key="txt08-deepseek-live", story=story)
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    evidence["run_id"] = run_id
    evidence["episode_id"] = episode["id"]
    retain(created.json())
    state = advance(client, settings, run_id)
    retain(state)
    assert state["status"] == "WAITING_REVIEW", f"See {evidence_file}; do not resend automatically"
    script_id = state["pending_review"]["version_id"]
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        frozen_input = digest(run.input_json)
        script = db.get(Script, UUID(script_id))
        evidence["script"] = script.content_json
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        assert (
            restarted.get(f"/api/v1/runs/{run_id}").json()["pending_review"]
            == state["pending_review"]
        )
    assert review(client, state).status_code == 202

    def quota_before_submission(*args, **kwargs):
        raise GatewayFailure("PROVIDER_QUOTA_EXHAUSTED", not_accepted=True)

    with monkeypatch.context() as injected:
        injected.setattr(OpenAICompatibleTextProvider, "storyboard", quota_before_submission)
        failed = advance(client, settings, run_id)
    retain(failed)
    assert failed["status"] == "FAILED" and failed["error_code"] == "GATEWAY_QUOTA_EXHAUSTED"
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    state = advance(client, settings, run_id)
    retain(state)
    assert state["status"] == "WAITING_REVIEW", f"See {evidence_file}; do not resend automatically"
    assert state["pending_review"]["resource_type"] == "storyboard"
    board_id = state["pending_review"]["version_id"]
    assert review(client, state).status_code == 202
    state = advance(client, settings, run_id)
    retain(state)
    assert state["status"] == "SUCCEEDED"
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    with client.app.state.session_factory() as db:
        assert digest(db.get(WorkflowRun, UUID(run_id)).input_json) == frozen_input
        assert db.scalar(select(func.count()).select_from(Script)) == 1
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1
        assert db.scalar(select(func.count()).select_from(Review)) == 2
        board = db.get(Storyboard, UUID(board_id))
        assert str(board.script_version_id) == script_id
        evidence["storyboard"] = board.content_json
    calls = client.get(api(episode) + "/calls").json()
    assert len(calls) == 3
    assert len(evidence["provider_responses"]) == 2
    assert [call["status"] for call in calls] == ["SUCCEEDED", "NOT_ACCEPTED", "SUCCEEDED"]
    assert all(call["maximum_cost"] is None for call in calls)
    evidence["cost_summary"] = client.get(f"/api/v1/projects/{episode['project_id']}/costs").json()[
        "summary"
    ]
    assert evidence["cost_summary"]["unpriced_count"] == 2
    retain(state)
