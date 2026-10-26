import copy
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_api_workflow import advance, review, start

from arclight.agents.contracts import ScriptContent, StoryboardContent
from arclight.application import runtime
from arclight.application.content_duration import duration_bounds, validate_duration
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.persistence.models import (
    AuditEvent,
    Episode,
    OutboxEvent,
    Review,
    Script,
    Shot,
    Storyboard,
    WorkflowRun,
)
from arclight.providers.text import MockTextProvider, OpenAICompatibleTextProvider


def script_content(duration=30000):
    return MockTextProvider().script("仅供隔离测试的故事", {}, duration).model_dump()


def save_script(client, episode, content=None, expected=0, **extra):
    return client.post(
        f"/api/v1/episodes/{episode['id']}/scripts",
        json={"expected_version_no": expected, "content": content or script_content(), **extra},
    )


def approve(client, episode, kind, version_id, decision="APPROVE"):
    return client.post(
        f"/api/v1/episodes/{episode['id']}/{kind}/{version_id}/review", json={"decision": decision}
    )


@pytest.mark.parametrize(
    "target,tolerance,expected",
    [
        (2500, 10, (2250, 2750)),
        (2501, 10, (2251, 2751)),
        (30001, 3, (29101, 30901)),
        (1800000, 10, (1620000, 1980000)),
        (30001, 0, (30001, 30001)),
        (2500, 100, (0, 5000)),
    ],
)
def test_integer_duration_bounds(target, tolerance, expected):
    assert duration_bounds(target, tolerance) == expected


@pytest.mark.parametrize("target,tolerance", [(0, 10), (-1, 10), (30000, -1), (30000, 101)])
def test_invalid_duration_policy(target, tolerance):
    with pytest.raises(ValueError):
        duration_bounds(target, tolerance)


@pytest.mark.parametrize("kind", ["script", "storyboard"])
@pytest.mark.parametrize(
    "value,valid", [(2250, False), (2251, True), (2501, True), (2751, True), (2752, False)]
)
def test_exact_boundaries_and_one_ms_outside(kind, value, valid):
    content = ScriptContent.model_validate(script_content(value))
    if kind == "storyboard":
        content = StoryboardContent.model_validate(
            {
                "shots": [
                    {
                        "shot_no": 1,
                        "scene_no": 1,
                        "duration_ms": value,
                        "visual_description": "边界镜头",
                    }
                ]
            }
        )
    before = content.model_dump()
    if valid:
        validate_duration(content, 2501, 10)
    else:
        with pytest.raises(DomainError, match="允许范围") as exc:
            validate_duration(content, 2501, 10)
        assert exc.value.status_code == 422
    assert content.model_dump() == before


@pytest.mark.parametrize("value", [True, 1.5, "30000", 0, -1])
def test_estimate_requires_positive_integer(value):
    with pytest.raises(ValidationError):
        ScriptContent.model_validate({**script_content(), "estimated_duration_ms": value})


@pytest.mark.parametrize("value", [True, 500.5, "6000"])
def test_shot_duration_does_not_silently_coerce(value):
    data = (
        MockTextProvider()
        .storyboard(ScriptContent.model_validate(script_content()), 30000)
        .model_dump()
    )
    data["shots"][0]["duration_ms"] = value
    with pytest.raises(ValidationError):
        StoryboardContent.model_validate(data)


@pytest.mark.parametrize("target", [2500, 2501, 30001, 150001, 1800000])
def test_mock_generation_exact_totals_at_short_long_and_rounding_targets(target):
    script = MockTextProvider().script("故事", {}, target)
    board = MockTextProvider().storyboard(script, target)
    assert script.estimated_duration_ms == target
    assert sum(s.duration_ms for s in board.shots) == target
    assert all(500 <= s.duration_ms <= 30000 for s in board.shots)
    assert len(board.shots) <= 100
    validate_duration(script, target, 0)
    validate_duration(board, target, 0)


def test_duration_configuration_from_environment(monkeypatch):
    monkeypatch.setenv("ARCLIGHT_TEXT_DURATION_TOLERANCE_PERCENT", "0")
    assert Settings(_env_file=None).text_duration_tolerance_percent == 0
    for invalid in (-1, 101):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, text_duration_tolerance_percent=invalid)


def test_script_rewrite_failure_is_atomic_and_original_remains(client, episode):
    prefix = f"/api/v1/episodes/{episode['id']}"
    first = save_script(client, episode).json()
    with client.app.state.session_factory() as db:
        audits = db.scalar(select(func.count()).select_from(AuditEvent))
    for duration in (26999, 33001, None):
        changed = {**first["content_json"], "estimated_duration_ms": duration, "title": "越界改写"}
        result = save_script(client, episode, changed, 1, base_version_id=first["id"])
        assert result.status_code == 422, result.text
        assert result.json()["error"]["code"] == (
            "SCRIPT_DURATION_REQUIRED" if duration is None else "SCRIPT_DURATION_OUT_OF_RANGE"
        )
    rows = client.get(prefix + "/scripts").json()
    assert len(rows) == 1 and rows[0]["content_json"] == first["content_json"]
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == audits
    corrected = save_script(
        client,
        episode,
        {**first["content_json"], "estimated_duration_ms": 33000},
        1,
        base_version_id=first["id"],
    )
    assert corrected.status_code == 201 and corrected.json()["version_no"] == 2


def test_storyboard_uses_server_sum_and_rewrites_atomically(client, episode):
    prefix = f"/api/v1/episodes/{episode['id']}"
    script = save_script(client, episode).json()
    assert approve(client, episode, "scripts", script["id"]).status_code == 200
    data = (
        MockTextProvider()
        .storyboard(ScriptContent.model_validate(script["content_json"]), 30000)
        .model_dump()
    )
    body = {"expected_version_no": 0, "script_version_id": script["id"], "content": data}
    first = client.post(prefix + "/storyboards", json=body)
    assert first.status_code == 201, first.text
    before = copy.deepcopy(data)
    for total in (26999, 33001):
        changed = copy.deepcopy(before)
        changed["shots"][0]["duration_ms"] += total - 30000
        response = client.post(
            prefix + "/storyboards",
            json={
                **body,
                "expected_version_no": 1,
                "base_version_id": first.json()["id"],
                "content": changed,
            },
        )
        assert (
            response.status_code == 422
            and response.json()["error"]["code"] == "STORYBOARD_DURATION_OUT_OF_RANGE"
        )
    spoof = client.post(
        prefix + "/storyboards", json={**body, "content": {**data, "total_duration_ms": 30000}}
    )
    assert spoof.status_code == 422
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1
        assert db.scalar(select(func.count()).select_from(Shot)) == 5
    assert client.get(prefix + "/storyboards").json()[0]["content_json"] == before


@pytest.mark.parametrize("kind", ["scripts", "storyboards"])
def test_target_change_revalidates_manual_approval_but_allows_reject(client, episode, kind):
    prefix = f"/api/v1/episodes/{episode['id']}"
    script = save_script(client, episode).json()
    version_id = script["id"]
    if kind == "storyboards":
        assert approve(client, episode, "scripts", version_id).status_code == 200
        board = (
            MockTextProvider()
            .storyboard(ScriptContent.model_validate(script["content_json"]), 30000)
            .model_dump()
        )
        version_id = client.post(
            prefix + "/storyboards",
            json={"expected_version_no": 0, "script_version_id": script["id"], "content": board},
        ).json()["id"]
    assert client.patch(prefix, json={"target_duration_ms": 60000}).status_code == 200
    result = approve(client, episode, kind, version_id)
    assert result.status_code == 422
    assert client.get(prefix + "/" + kind).json()[0]["status"] == "READY_FOR_REVIEW"
    assert approve(client, episode, kind, version_id, "REJECT").status_code == 200


def test_legacy_read_and_resave_require_explicit_estimate(client, episode):
    old = script_content()
    old.pop("schema_version")
    old.pop("estimated_duration_ms")
    with client.app.state.session_factory.begin() as db:
        row = Script(
            episode_id=UUID(episode["id"]),
            version_no=1,
            content_json=old,
            status="READY_FOR_REVIEW",
        )
        db.add(row)
        db.flush()
        version_id = str(row.id)
    prefix = f"/api/v1/episodes/{episode['id']}"
    read = client.get(prefix + "/scripts").json()[0]
    assert read["content_json"]["estimated_duration_ms"] is None
    assert approve(client, episode, "scripts", version_id).status_code == 422
    with client.app.state.session_factory.begin() as db:
        row = db.get(Script, UUID(version_id))
        assert row.content_json == old
        row.status = "APPROVED"
    blocked = client.post(
        prefix + "/storyboard-runs",
        headers={"Idempotency-Key": "legacy"},
        json={"script_version_id": version_id},
    )
    assert (
        blocked.status_code == 422 and blocked.json()["error"]["code"] == "SCRIPT_DURATION_REQUIRED"
    )
    changed = {**read["content_json"], "estimated_duration_ms": 30000}
    corrected = save_script(client, episode, changed, 1, base_version_id=version_id)
    assert corrected.status_code == 201
    assert (
        corrected.json()["content_json"]["scenes"][0]["scene_id"]
        == read["content_json"]["scenes"][0]["scene_id"]
    )
    with client.app.state.session_factory() as db:
        assert db.get(Script, UUID(version_id)).content_json == old
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 0


@pytest.mark.parametrize("stage", ["script", "storyboard"])
def test_fake_over_duration_generation_fails_without_artifact_and_explicit_retry_recovers(
    client, episode, settings, monkeypatch, stage
):
    class Broken(MockTextProvider):
        def script(self, story, style, duration_ms=30000, *, story_settings=None):
            content = super().script(story, style, duration_ms)
            if stage == "script":
                content.estimated_duration_ms = duration_ms * 2
            return content

        def storyboard(self, script, duration_ms):
            content = super().storyboard(script, duration_ms)
            content.shots[0].duration_ms = 30000
            return content

    monkeypatch.setattr(runtime, "build_provider", lambda _: Broken())
    run_id = start(client, episode).json()["run_id"]
    state = advance(client, settings, run_id)
    if stage == "storyboard":
        assert review(client, state).status_code == 202
        state = advance(client, settings, run_id)
    assert state["status"] == "FAILED"
    assert state["error_code"] == stage.upper() + "_DURATION_OUT_OF_RANGE"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Script)) == (
            0 if stage == "script" else 1
        )
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.scalar(select(func.count()).select_from(Shot)) == 0
    monkeypatch.setattr(runtime, "build_provider", lambda _: MockTextProvider())
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    recovered = advance(client, settings, run_id)
    assert recovered["status"] == "WAITING_REVIEW"
    assert recovered["pending_review"]["resource_type"] == stage


@pytest.mark.parametrize("stage", ["script", "storyboard"])
def test_workflow_approval_rechecks_without_consuming_revision_or_review(
    client, episode, settings, stage
):
    run_id = start(client, episode).json()["run_id"]
    state = advance(client, settings, run_id)
    if stage == "storyboard":
        assert review(client, state).status_code == 202
        state = advance(client, settings, run_id)
    with client.app.state.session_factory.begin() as db:
        model = Script if stage == "script" else Storyboard
        artifact = db.get(model, UUID(state["pending_review"]["version_id"]))
        content = copy.deepcopy(artifact.content_json)
        if stage == "script":
            content["estimated_duration_ms"] = 60000
        else:
            content["shots"][0]["duration_ms"] = 30000
        artifact.content_json = content
        reviews = db.scalar(select(func.count()).select_from(Review))
        outbox = db.scalar(select(func.count()).select_from(OutboxEvent))
    rejected = review(client, state)
    assert rejected.status_code == 422
    current = client.get(f"/api/v1/runs/{run_id}").json()
    assert (
        current["revision"] == state["revision"]
        and current["pending_review"] == state["pending_review"]
    )
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Review)) == reviews
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == outbox
    assert review(client, state, "REJECT").status_code == 202
    assert advance(client, settings, run_id)["status"] == "REJECTED"


@pytest.mark.parametrize("target", [2501, 1800000])
def test_full_mock_workflow_short_long_targets_and_zero_tolerance(
    client, episode, settings, target
):
    prefix = f"/api/v1/episodes/{episode['id']}"
    assert client.patch(prefix, json={"target_duration_ms": target}).status_code == 200
    settings.text_duration_tolerance_percent = 0
    run_id = start(client, episode).json()["run_id"]
    first = advance(client, settings, run_id)
    assert first["status"] == "WAITING_REVIEW", first
    assert review(client, first).status_code == 202
    second = advance(client, settings, run_id)
    assert second["status"] == "WAITING_REVIEW", second
    assert review(client, second).status_code == 202
    assert advance(client, settings, run_id)["status"] == "SUCCEEDED"
    assert (
        sum(
            s["duration_ms"]
            for s in client.get(prefix + "/storyboards").json()[0]["content_json"]["shots"]
        )
        == target
    )


def test_provider_receives_target_range_as_data(settings, monkeypatch):
    settings.text_duration_tolerance_percent = 3
    requests = []
    script = script_content(30001)
    board = MockTextProvider().storyboard(ScriptContent.model_validate(script), 30001).model_dump()
    import json

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(script if len(requests) == 1 else board)}}
                ]
            },
        )

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    provider = OpenAICompatibleTextProvider(settings)
    generated = provider.script("故事", {}, 30001)
    provider.storyboard(generated, 30001)
    for body in requests:
        data = json.loads(body["messages"][1]["content"])
        assert data["target_duration_ms"] == 30001
        assert data["duration_range_ms"] == [29101, 30901]


def test_manual_saves_honor_configured_zero_tolerance(client, episode, settings):
    settings.text_duration_tolerance_percent = 0
    assert save_script(client, episode, script_content(30001)).status_code == 422
    script = save_script(client, episode).json()
    assert approve(client, episode, "scripts", script["id"]).status_code == 200
    board = MockTextProvider().storyboard(
        ScriptContent.model_validate(script["content_json"]), 30001
    )
    payload = {
        "script_version_id": script["id"],
        "expected_version_no": 0,
        "content": board.model_dump(),
    }
    url = f"/api/v1/episodes/{episode['id']}/storyboards"
    assert client.post(url, json=payload).status_code == 422
    payload["content"]["shots"][0]["duration_ms"] -= 1
    assert client.post(url, json=payload).status_code == 201


@pytest.mark.parametrize(
    "estimate,code", [(None, "SCRIPT_DURATION_REQUIRED"), (60000, "SCRIPT_DURATION_OUT_OF_RANGE")]
)
def test_director_rechecks_source_before_provider_call(
    client, episode, settings, monkeypatch, estimate, code
):
    script = save_script(client, episode).json()
    assert approve(client, episode, "scripts", script["id"]).status_code == 200
    result = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboard-runs",
        headers={"Idempotency-Key": "source-drift"},
        json={"script_version_id": script["id"]},
    )
    assert result.status_code == 202
    with client.app.state.session_factory.begin() as db:
        row = db.get(Script, UUID(script["id"]))
        row.content_json = {**row.content_json, "estimated_duration_ms": estimate}
    calls = []

    class NeverCalled(MockTextProvider):
        def storyboard(self, source, duration_ms):
            calls.append(duration_ms)
            return super().storyboard(source, duration_ms)

    monkeypatch.setattr(runtime, "build_provider", lambda _: NeverCalled())
    state = advance(client, settings, result.json()["run_id"])
    assert state["status"] == "FAILED" and state["error_code"] == code
    assert calls == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.scalar(select(func.count()).select_from(Shot)) == 0


def test_workflow_generation_and_approval_use_run_target_snapshot(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    # Simulate external drift only in the isolated fixture; normal API blocks edits during a run.
    with client.app.state.session_factory.begin() as db:
        db.get(Episode, UUID(episode["id"])).target_duration_ms = 60000
    first = advance(client, settings, run_id)
    assert first["status"] == "WAITING_REVIEW"
    assert review(client, first).status_code == 202
    second = advance(client, settings, run_id)
    assert second["status"] == "WAITING_REVIEW"
    assert review(client, second).status_code == 202
    assert advance(client, settings, run_id)["status"] == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(Script)).content_json["estimated_duration_ms"] == 30000
        assert sum(shot.duration_ms for shot in db.scalars(select(Shot))) == 30000
