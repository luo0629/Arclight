"""Actual formal renders and separate Owner content decisions gate series completion."""

import copy
from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import render_case, timeline_case
from render_report_helpers import accept_warnings, completed_report
from sqlalchemy import select
from test_continuity_reviews import review_case
from test_narrative_review import execute, human_decision, progress, start
from test_producer_series import begin
from test_script_state_commit import capture, screenplay
from test_series_planning import planned_outline as planned_outline  # noqa: F401
from test_writing_context import writing_case as writing_case  # noqa: F401

from arclight.agents.contracts import legacy_script_view, read_script_content
from arclight.application.producer_series_delivery import complete_series, public_series_progress
from arclight.core.errors import DomainError
from arclight.domain.producer_series import SeriesCompletion
from arclight.persistence.models import AssetVersion, StoryLedgerHead, WorkflowRun


def current_baseline(client, case):
    with client.app.state.session_factory() as db:
        head = db.scalar(
            select(StoryLedgerHead).where(
                StoryLedgerHead.project_id == UUID(case["episode"]["project_id"])
            )
        )
        return {
            "series_plan_version_id": case["plan"]["id"],
            "ledger_version_id": str(head.approved_version_id) if head else None,
            "expected_ledger_head_revision": head.revision if head else 0,
        }


@pytest.fixture
def delivered_series(client, settings, writing_case, monkeypatch):
    import test_continuity_reviews
    from test_text_contract_v2 import save_script

    case = copy.deepcopy(writing_case)
    episodes = [case["episode"]]
    for number in (2, 3):
        created = client.post(
            f"/api/v1/projects/{case['episode']['project_id']}/episodes",
            json={"episode_no": number, "title": f"第{number}集", "target_duration_ms": 30000},
        )
        assert created.status_code == 201, created.text
        episodes.append(created.json())
    original_board = test_continuity_reviews.approved_board

    def v3_script(api, episode):
        target = copy.deepcopy(case)
        target["episode"] = episode
        target["request"].update(current_baseline(api, case))
        target["request"]["target_story_time"] = episode["episode_no"] * 10
        content = screenplay(target, capture(api, target))
        content["scenes"][0]["state_changes"] = []
        return save_script(api, episode, content=content)

    def board(api, episode, script):
        value = legacy_script_view(read_script_content(script["content_json"]))
        return original_board(
            api, episode, script | {"content_json": value.model_dump(mode="json")}
        )

    monkeypatch.setattr(test_continuity_reviews, "save_script", v3_script)
    monkeypatch.setattr(test_continuity_reviews, "approved_board", board)
    targets, deliveries, timelines = [], [], []
    for episode in episodes:
        review = review_case.__wrapped__(client, episode)
        timeline = timeline_case.__wrapped__(client, episode, review)
        ready = render_case.__wrapped__(client, episode, timeline)
        manifest, job, report = completed_report(client, episode, ready)
        assert job["status"] == "SUCCEEDED"
        if not report["acceptance"]["is_delivery"]:
            accept_warnings(client, episode, job["artifact_id"])
        with client.app.state.session_factory() as db:
            from arclight.persistence.models import Storyboard

            board_row = db.get(Storyboard, UUID(review["target"]["storyboard_id"]))
            targets.append(
                {
                    "script_version_id": str(board_row.script_version_id),
                    "storyboard_version_id": str(board_row.id),
                }
            )
        deliveries.append({"episode_id": episode["id"], "artifact_id": job["artifact_id"]})
        timelines.append(ready)
    case["request"].update(current_baseline(client, case))
    case["scope"] = current_baseline(client, case) | {
        "episodes": [{"episode_id": item["id"]} for item in episodes],
        "confirmation": "确认制作并连续查看三集完整作品。",
    }
    case["targets"], case["deliveries"] = targets, deliveries
    case["timelines"] = timelines
    return case


def reviewed(client, settings, case, *, approve=True):
    body = {
        "creation_key": str(uuid4()),
        "scope": "SERIES",
        "baseline": current_baseline(client, case),
        "targets": case["targets"],
    }
    run_id = start(client, case, body)
    execute(client, settings, run_id)
    value = progress(client, case, run_id)
    assert value["status"] == "WAITING_REVIEW", value
    if approve:
        decision = human_decision(value)
        human_id = value["human_review"]["id"]
        saved = client.post(f"/api/v1/human-reviews/{human_id}/decisions", json=decision)
        assert saved.status_code == 200, saved.text
        resumed = client.post(
            f"/api/v1/human-reviews/{human_id}/resume",
            json={"decision_id": saved.json()["id"], "request_sha256": decision["request_sha256"]},
        )
        assert resumed.status_code == 200, resumed.text
    return value["report"]["report_id"]


def completion(client, run_id, case, report_id, **changes):
    with client.app.state.session_factory() as db:
        revision = db.get(WorkflowRun, run_id).revision
    return SeriesCompletion.model_validate(
        {
            "expected_revision": revision,
            "scope": "SERIES",
            "deliveries": case["deliveries"],
            "narrative_report_id": report_id,
            "continuous_viewing_confirmed": True,
            "comment": "离线测试：Owner 明确核对正式成片与内容限制，不声称真实模型艺术质量。",
            **changes,
        }
    )


def test_actual_three_episode_delivery_requires_content_decision_and_remains_version_bound(
    client, settings, delivered_series
):
    case = delivered_series
    run_id, actor = begin(client, settings, case)
    report_id = reviewed(client, settings, case, approve=False)
    payload = completion(client, run_id, case, report_id)
    with client.app.state.session_factory.begin() as db, pytest.raises(DomainError) as failure:
        complete_series(db, run_id, actor, payload)
    assert failure.value.code == "PRODUCER_CONTENT_APPROVAL_REQUIRED"
    # Apply the independent review, rather than mocking a report or toggling source state.
    with client.app.state.session_factory() as db:
        from arclight.application.narrative_review import read_report

        report = read_report(db, UUID(case["episode"]["project_id"]), UUID(report_id))
        review_run = str(report.run_id)
    value = progress(client, case, review_run)
    answer = human_decision(value)
    human_id = value["human_review"]["id"]
    decision = client.post(f"/api/v1/human-reviews/{human_id}/decisions", json=answer)
    assert decision.status_code == 200, decision.text
    resumed = client.post(
        f"/api/v1/human-reviews/{human_id}/resume",
        json={"decision_id": decision.json()["id"], "request_sha256": answer["request_sha256"]},
    )
    assert resumed.status_code == 200, resumed.text
    response = client.post(
        f"/api/v1/runs/{run_id}/producer/completion", json=payload.model_dump(mode="json")
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "SUCCEEDED"
    assert (
        client.post(
            f"/api/v1/runs/{run_id}/producer/completion", json=payload.model_dump(mode="json")
        ).status_code
        == 202
    )
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        current = public_series_progress(db, run, actor)
        assert current["whole_series_complete"] and current["completion_current"]
        assert len(current["completion"]["deliveries"]) == 3
    with client.app.state.session_factory.begin() as db:
        from arclight.persistence.models import RenderArtifact

        artifact = db.get(RenderArtifact, UUID(case["deliveries"][0]["artifact_id"]))
        db.get(AssetVersion, artifact.asset_version_id).stale = True
    with client.app.state.session_factory() as db:
        current = public_series_progress(db, db.get(WorkflowRun, run_id), actor)
        assert not current["whole_series_complete"] and not current["completion_current"]
        assert current["completion"]["whole_series_complete"]  # Historical receipt is unchanged.


def test_partial_batch_cannot_claim_whole_series_and_missing_or_preview_work_never_completes(
    client, settings, delivered_series
):
    case = delivered_series
    report_id = reviewed(client, settings, case)
    case["scope"]["episodes"] = case["scope"]["episodes"][:1]
    case["deliveries"] = case["deliveries"][:1]
    run_id, actor = begin(client, settings, case)
    payload = completion(client, run_id, case, report_id)
    with client.app.state.session_factory.begin() as db, pytest.raises(DomainError) as failure:
        complete_series(db, run_id, actor, payload)
    assert failure.value.code == "PRODUCER_DELIVERY_SCOPE"
    missing = completion(
        client,
        run_id,
        case,
        report_id,
        scope="BATCH",
        deliveries=[{"episode_id": case["episode"]["id"], "artifact_id": str(uuid4())}],
    )
    with client.app.state.session_factory.begin() as db, pytest.raises(DomainError) as failure:
        complete_series(db, run_id, actor, missing)
    assert failure.value.code == "NOT_FOUND"
    from render_admission_helpers import ready_manifest
    from render_job_helpers import read, run, submit

    preview_manifest, _ = ready_manifest(client, case["timelines"][0], profile="PREVIEW")
    preview_job, _ = submit(client, preview_manifest)
    assert run(client, preview_job) == "SUCCEEDED"
    preview = read(client, preview_job)
    preview_payload = completion(
        client,
        run_id,
        case,
        report_id,
        scope="BATCH",
        deliveries=[
            {
                "episode_id": case["episode"]["id"],
                "artifact_id": preview["artifact_id"],
            }
        ],
    )
    with client.app.state.session_factory.begin() as db, pytest.raises(DomainError) as failure:
        complete_series(db, run_id, actor, preview_payload)
    assert failure.value.code == "PRODUCER_FORMAL_DELIVERY_REQUIRED"
    batch = completion(client, run_id, case, report_id, scope="BATCH")
    with client.app.state.session_factory.begin() as db:
        complete_series(db, run_id, actor, batch)
        value = public_series_progress(db, db.get(WorkflowRun, run_id), actor)
        assert value["completion_current"] and not value["whole_series_complete"]
        assert value["completion"]["phase"] == "BATCH_COMPLETED"
