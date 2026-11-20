"""The series workspace reads authoritative, project-scoped history without executing work."""

from uuid import UUID

import pytest
from starlette.testclient import TestClient
from test_collaboration import register
from test_producer_series import begin
from test_producer_series import series_case as series_case  # noqa: F401
from test_series_planning import planned_outline as planned_outline  # noqa: F401
from test_writing_context import ledger_baseline
from test_writing_context import writing_case as writing_case  # noqa: F401

from arclight.main import create_app
from arclight.persistence.models import ProjectMember


def test_exact_approved_plan_paged_paused_run_and_resume_survive_new_app(
    client, settings, series_case
):
    case = series_case
    project_id = case["episode"]["project_id"]
    ledger_baseline(client, case)
    case["scope"].update(
        {
            key: case["request"][key]
            for key in ("ledger_version_id", "expected_ledger_head_revision")
        }
    )
    run_id, _ = begin(client, settings, case)
    progress_url = f"/api/v1/runs/{run_id}/producer"
    catalog_url = f"/api/v1/projects/{project_id}/series-overview"
    before = client.get(progress_url).json()
    control_url = f"{progress_url}/series-controls"
    receipt = client.post(
        control_url,
        headers={"Idempotency-Key": "s08-pause"},
        json={"action": "PAUSE", "reason": "验收持久暂停", "expected_revision": before["revision"]},
    )
    assert receipt.status_code == 202, receipt.text
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.update(client.cookies)
        restarted.headers["Authorization"] = client.headers["Authorization"]
        response = restarted.get(catalog_url + "?limit=1&offset=0")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        value = response.json()
        assert value["plan"]["id"] == case["plan"]["id"]
        assert value["plan"]["characters"][0]["name"] == case["characters"][0]["name"]
        assert value["ledger_version_id"] == case["request"]["ledger_version_id"]
        assert value["ledger_head_revision"] == case["request"]["expected_ledger_head_revision"]
        assert value["total"] == value["limit"] == 1
        assert value["items"][0]["id"] == str(run_id) and value["items"][0]["paused"]
        assert restarted.get(catalog_url + "?limit=1&offset=1").json()["items"] == []
        assert not any(
            field in response.text for field in ("input_json", "idempotency_key", "secret")
        )
        paused = restarted.get(progress_url).json()
        resumed = restarted.post(
            control_url,
            headers={"Idempotency-Key": "s08-resume"},
            json={
                "action": "RESUME",
                "reason": "重新读取后恢复原运行",
                "expected_revision": paused["revision"],
            },
        )
        assert resumed.status_code == 202, resumed.text
        assert resumed.json()["run_id"] == str(run_id)
        assert not restarted.get(catalog_url).json()["items"][0]["paused"]
        after = restarted.get(progress_url).json()
        assert after["budget"] == before["budget"]


def test_catalog_and_episode_evidence_respect_project_access(client, episode):
    project_id = episode["project_id"]
    paths = [
        f"/api/v1/projects/{project_id}/series-overview",
        f"/api/v1/episodes/{episode['id']}/series-evidence",
    ]
    approved, evidence = [client.get(path).json() for path in paths]
    assert approved["plan"] is None and approved["total"] == 0
    assert evidence["episode_id"] == episode["id"] and evidence["project_id"] == project_id
    assert evidence["script_version_id"] is None and evidence["delivery_id"] is None
    assert not evidence["delivery_reviewed"] and evidence["audio_snapshot_id"] is None
    assert evidence["performance_shots"] == evidence["reviewed_dynamic_shots"] == 0
    viewer = register(client, "series-workspace-viewer@example.test")
    assert all(client.get(path).status_code == 404 for path in paths)
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(project_id), user_id=UUID(viewer["user"]["id"]), role="viewer"
            )
        )
    assert all(client.get(path).status_code == 200 for path in paths)
    rejected = client.post(
        f"/api/v1/projects/{project_id}/producer-runs",
        headers={"Idempotency-Key": "forbidden-start"},
        json={
            "goal": "只读角色不得发起",
            "scope": {
                "series_plan_version_id": episode["id"],
                "ledger_version_id": None,
                "expected_ledger_head_revision": 0,
                "episodes": [{"episode_id": episode["id"]}],
                "confirmation": "不允许的操作",
            },
        },
    )
    assert rejected.status_code == 403, rejected.text


@pytest.mark.parametrize("query", ["?limit=0", "?limit=101", "?offset=-1"])
def test_catalog_bounded_page(client, episode, query):
    response = client.get(f"/api/v1/projects/{episode['project_id']}/series-overview{query}")
    assert response.status_code == 422
