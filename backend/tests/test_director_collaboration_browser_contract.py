"""Check the browser HTTP fixture against real services without starting any server."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_agent_model import model_service as model_service
from test_director_creation import approved_script, progress
from test_director_revision import answer_body
from test_screenwriter_creation import approve, run_once

from arclight.domain.model_gateway import GatewayPolicy


@pytest.mark.parametrize(
    "note,decision", [("", "ADOPT"), ("拒绝美术", "DECLINE"), ("美术澄清", "ADOPT")]
)
def test_browser_http_fixture_uses_actual_collaboration_contract(
    client, episode, settings, model_service, monkeypatch, note, decision
):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    from e2e_screenwriter import _respond

    model_service(_respond)
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost="0", pricing_version="direct04-browser:1"
    )

    def configure(content):
        content["scenes"][0]["notes"] = note

    source = approved_script(client, episode, count=1, configure=configure)
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboard-runs",
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "script_version_id": source["id"],
            "professional": True,
            "director_revision_rounds": 2,
            "director_art_collaboration": True,
        },
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    run = run_once(client, settings, run_id)
    if note == "美术澄清":
        assert run["status"] == "RUNNING", run
        waiting = next(
            op for op in progress(client, run_id)["operations"] if op["kind"] == "art_opinion"
        )
        assert waiting["human_request"]["question"] == "美术方案采用冷色还是暖色？"
        response = client.post(
            f"/api/v1/runs/{run_id}/director/{waiting['loop_id']}/answers",
            json=answer_body(waiting) | {"answer": "冷色"},
        )
        assert response.status_code == 202, response.text
        run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, progress(client, run_id))
    data = progress(client, run_id)
    checks = [op for op in data["operations"] if op["kind"] == "director_check"]
    assert checks[0]["result"]["opinion_decision"]["decision"] == decision
    assert data["art_opinion_request"]["storyboard_status"] == "WORK_IN_PROGRESS"
    approve(client, run)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED"
