from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from art_direction_support import install_direction_model, reference_set
from sqlalchemy import select
from test_collaboration import create_episode, create_project, register
from test_director_revision import install_revision
from test_native_media_integration import catalog_version, configure
from test_producer_planning import install_plan_model
from test_producer_planning import model_service as model_service
from test_short_drama_delivery import (
    approve_recordings,
    authorized_run,
    bind_delivery_visuals,
    delivery_font,
)
from test_short_drama_delivery import (
    review_case as review_case,
)
from test_short_drama_delivery import (
    timeline_case as timeline_case,
)
from test_text_contract_v2 import legacy_script, save_script

from arclight.application.agent_limits import action_usage
from arclight.application.agent_workflows import WorkflowRecoveryFailed
from arclight.application.audio_voice_catalog import query_voice_catalog, read_voice_catalog_source
from arclight.application.producer_interaction import command_rows
from arclight.application.production_versions import find_binding
from arclight.application.render_execution import execute_render
from arclight.application.runtime import execute_run
from arclight.application.short_drama_audio import (
    DIALOGUE_PLAN,
    publish_voice_casting,
    read_voice_casting_input,
)
from arclight.application.short_drama_delivery import queue_short_drama_delivery
from arclight.application.short_drama_diagnostics import DETAIL, ShortDramaFailure
from arclight.application.short_drama_production import advance_pending, set_progress
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.timelines import TimelineCreate
from arclight.domain.versions import SHORT_DRAMA_VOICE_INPUT
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.base import utcnow
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import (
    AuditEvent,
    OutboxEvent,
    Project,
    Script,
    Storyboard,
    WorkflowRecoveryCounter,
    WorkflowRun,
)
from arclight.persistence.render_manifests import RenderArtifact
from arclight.workers import tasks as worker_tasks
from arclight.workers.tasks import pending_workflow_events


def create_short_drama(client, episode, script, *, key=None, **overrides):
    return client.post(
        f"/api/v1/episodes/{episode['id']}/short-drama",
        headers={"Idempotency-Key": key or uuid4().hex},
        json={
            "script_version_id": script["id"],
            "generated_media_rights_confirmed": True,
            **overrides,
        },
    )


def progress(client, episode):
    return client.get(f"/api/v1/episodes/{episode['id']}/short-drama").json()


@pytest.fixture
def configured_episode(client, settings):
    register(client, "short-drama-configured@example.com")
    episode = create_episode(client, create_project(client))
    configure(client, settings)
    return episode


def test_preflight_identifies_missing_models_before_any_work(client, settings):
    register(client, "short-drama-preflight@example.com")
    episode = create_episode(client, create_project(client))
    content = legacy_script()
    content["scenes"][0]["dialogue"] = []
    script = save_script(client, episode, content=content, approve=False)
    settings.provider = "account"

    readiness = client.get(
        f"/api/v1/episodes/{episode['id']}/short-drama/preflight",
        params={"script_version_id": script["id"]},
    )
    assert readiness.status_code == 200, readiness.text
    assert [
        (issue["phase"], issue["service_kind"], issue["code"])
        for issue in readiness.json()["issues"]
    ] == [
        ("PLANNING", "TEXT", "SHORT_DRAMA_TEXT_SERVICE_REQUIRED"),
        ("SHOTS", "IMAGE", "SHORT_DRAMA_VISUAL_SERVICE_REQUIRED"),
    ]
    denied = create_short_drama(client, episode, script)
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "SHORT_DRAMA_TEXT_SERVICE_REQUIRED"
    assert progress(client, episode) is None
    with client.app.state.session_factory() as db:
        assert db.get(Script, UUID(script["id"])).status == "READY_FOR_REVIEW"


def test_saved_failure_details_are_visible_only_to_the_service_owner(client, configured_episode):
    episode = configured_episode
    auth = client.headers["Authorization"]
    content = legacy_script()
    content["scenes"][0]["dialogue"] = []
    script = save_script(client, episode, content=content)
    queued = create_short_drama(client, episode, script)
    assert queued.status_code == 202, queued.text
    run_id = UUID(queued.json()["run_id"])
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        issue = ShortDramaFailure(
            DomainError("GATEWAY_PROVIDER_AUTHENTICATION_FAILED", "internal provider details"),
            service_kind="IMAGE",
            service={"supplier": "private-supplier", "model": "private-model"},
            service_owner=run.input_json["created_by"],
        )
        run.status, run.error_code = "FAILED", issue.code
        set_progress(db, run, "FAILED", stopped_phase="SHOTS", **{DETAIL: issue.record("SHOTS")})
    assert progress(client, episode)["issue"] == {
        "phase": "SHOTS",
        "code": "GATEWAY_PROVIDER_AUTHENTICATION_FAILED",
        "service_kind": "IMAGE",
        "supplier": "private-supplier",
        "model": "private-model",
    }
    member_email = "short-drama-diagnostic-viewer@example.com"
    member = register(client, member_email)
    client.headers["Authorization"] = auth
    added = client.post(
        f"/api/v1/projects/{episode['project_id']}/members",
        json={"email": member_email, "role": "viewer"},
    )
    assert added.status_code == 201, added.text
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    issue = progress(client, episode)["issue"]
    assert issue["code"] == "GATEWAY_PROVIDER_AUTHENTICATION_FAILED"
    assert issue["supplier"] is None and issue["model"] is None
    events = client.get(f"/api/v1/runs/{run_id}/events")
    assert events.status_code == 200, events.text
    assert "private-model" not in events.text and DETAIL not in events.text


def test_confirmation_is_atomic_and_replay_preserves_exact_grant(
    client, configured_episode, settings
):
    episode = configured_episode
    script = save_script(client, episode, approve=False)
    font_id = delivery_font(client, episode, script)
    options = {"font_file_id": str(font_id)}
    denied = create_short_drama(
        client, episode, script, generated_media_rights_confirmed=False, **options
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "SHORT_DRAMA_RIGHTS_REQUIRED"
    denied = create_short_drama(client, episode, script, **options)
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "SHORT_DRAMA_SPEECH_SERVICE_REQUIRED"
    assert progress(client, episode) is None
    factory = client.app.state.session_factory
    with factory() as db:
        assert db.get(Script, UUID(script["id"])).status == "READY_FOR_REVIEW"
        assert db.scalar(select(WorkflowRun.id)) is None

    content = legacy_script()
    content["scenes"][0]["dialogue"] = []
    edited = client.post(
        f"/api/v1/episodes/{episode['id']}/scripts",
        json={"expected_version_no": 1, "content": {"estimated_duration_ms": 30000, **content}},
    )
    assert edited.status_code == 201, edited.text
    script = edited.json()
    key = uuid4().hex
    queued = create_short_drama(client, episode, script, key=key)
    assert queued.status_code == 202, queued.text
    assert create_short_drama(client, episode, script, key=key).json() == queued.json()
    conflict = create_short_drama(client, episode, script, key=key, font_file_id=str(uuid4()))
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    run_id = UUID(queued.json()["run_id"])
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert db.get(Script, UUID(script["id"])).status == "APPROVED"
        commands = command_rows(db, run, UUID(episode["project_id"]))
        assert [command.snapshot_json["payload"] for command in commands] == [{"action": "PLAN"}]
        grant = db.scalar(select(AuditEvent).where(AuditEvent.action == "short_drama.authorized"))
        assert grant.details_json == run.input_json["short_drama"]
    assert progress(client, episode)["phase"] == "PLANNING"
    advance_pending(factory, settings)
    assert progress(client, episode)["phase"] == "PLANNING"


def test_admission_freezes_only_missing_voice_roles(client, configured_episode, settings):
    episode = configured_episode
    project_id = UUID(episode["project_id"])
    configure(client, settings, "speech", "openai_tts")
    content = legacy_script()
    content["characters"].append("访客")
    content["scenes"][0]["dialogue"].append({"speaker": "访客", "text": "等候多时。"})
    script = save_script(client, episode, content=content, approve=False)

    def save_voice(speaker, voice):
        saved = client.post(
            f"/api/v1/projects/{project_id}/audio-voices",
            json={
                "creation_key": str(uuid4()),
                "speaker": speaker,
                "expected_version_no": 0,
                "model_config_version": catalog_version(client, project_id),
                "parameters": {"voice": voice},
            },
        )
        assert saved.status_code == 201, saved.text
        return saved.json()["id"]

    existing = save_voice("主角", "test-voice")
    key = uuid4().hex
    queued = create_short_drama(client, episode, script, key=key)
    assert queued.status_code == 202, queued.text
    replay = create_short_drama(client, episode, script, key=key)
    assert replay.json() == queued.json()
    run_id = UUID(queued.json()["run_id"])
    factory = client.app.state.session_factory
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        casting = read_voice_casting_input(db, project_id, run_id)
        assert {(role.speaker, role.character_asset_id) for role in casting.roles} == {
            ("旁白", None),
            ("访客", None),
        }
        binding = find_binding(db, project_id, SHORT_DRAMA_VOICE_INPUT, run_id)
        assert binding is not None
        frozen = binding.snapshot_json["dialogue"]
        assert {voice["voice_version_id"] for voice in frozen["voices"].values()} == {existing}
        assert find_binding(db, project_id, DIALOGUE_PLAN, run_id) is None
        assert [row.snapshot_json["action"] for row in command_rows(db, run, project_id)] == [
            "PLAN"
        ]
        catalog = query_voice_catalog(
            read_voice_catalog_source(db, settings, project_id, run.input_json["created_by"])
        )

    private = client.get(
        f"/api/v1/projects/{project_id}/production-versions/bindings",
        params={"resource_id": str(run_id), "resource_type": SHORT_DRAMA_VOICE_INPUT},
    )
    assert private.status_code == 200, private.text
    assert private.json() == []

    configure(client, settings, "speech", "openai_tts", revision=1, key="rotated-casting-test")
    manual = save_voice("旁白", "test-voice")
    choices = {role.role_id: "another-voice" for role in casting.roles}
    for _ in range(2):
        with factory.begin() as db:
            run = db.get(WorkflowRun, run_id, with_for_update=True)
            publish_voice_casting(db, settings, run, choices, catalog=catalog)
            versions = {
                row.speaker: row
                for row in db.scalars(
                    select(AudioVoiceVersion).where(AudioVoiceVersion.project_id == project_id)
                )
            }
            assert set(versions) == {"主角", "旁白", "访客"}
            assert {row.version_no for row in versions.values()} == {1}
            assert str(versions["主角"].id) == existing
            assert str(versions["旁白"].id) == manual
            assert versions["访客"].configuration_json["parameters"] == {"voice": "another-voice"}
            assert (
                versions["访客"].configuration_json["model_config_version"]
                == catalog.configuration_version
            )
            exact = find_binding(db, project_id, DIALOGUE_PLAN, run_id).snapshot_json
            for scene in script["content_json"]["scenes"]:
                for line in scene["dialogue"]:
                    assert exact["voices"][line["line_id"]]["voice_version_id"] == str(
                        versions[line["speaker"]].id
                    )


def test_only_project_owner_can_authorize_short_drama(client):
    owner = register(client, "short-drama-owner@example.com")
    project = create_project(client)
    episode = create_episode(client, project)
    script = save_script(client, episode, approve=False)
    editor = register(client, "short-drama-editor@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    member = client.post(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": "short-drama-editor@example.com", "role": "editor"},
    )
    assert member.status_code == 201, member.text
    client.headers["Authorization"] = "Bearer " + editor["access_token"]
    rejected = create_short_drama(client, episode, script)
    assert rejected.status_code == 403, rejected.text
    assert progress(client, episode) is None


def test_repeated_recovery_failures_pause_original_delivery_across_broker_messages(
    client, configured_episode, settings, monkeypatch
):
    from arclight.application import producer_execution

    episode = configured_episode
    content = legacy_script()
    content["scenes"][0]["dialogue"] = []
    script = save_script(client, episode, content=content)
    accepted = create_short_drama(client, episode, script)
    assert accepted.status_code == 202, accepted.text
    run_id = UUID(accepted.json()["run_id"])
    factory = client.app.state.session_factory
    with factory() as db:
        original = db.scalar(select(OutboxEvent).where(OutboxEvent.run_id == run_id))
        revision, outbox_id = original.revision, original.id
    failures = []

    def unavailable(*_):
        failures.append(1)
        raise RuntimeError("synthetic recovery failure")

    monkeypatch.setattr(worker_tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_tasks, "session_factory", lambda *_: factory)
    monkeypatch.setattr(producer_execution, "_invoke", unavailable)
    for _ in range(worker_tasks.MAX_WORKFLOW_RECOVERY_FAILURES - 1):
        with pytest.raises(WorkflowRecoveryFailed):
            worker_tasks.execute_workflow.run(str(run_id), revision)
    worker_tasks.execute_workflow.run(str(run_id), revision)
    worker_tasks.execute_workflow.run(str(run_id), revision)
    assert len(failures) == worker_tasks.MAX_WORKFLOW_RECOVERY_FAILURES
    with factory() as db:
        assert db.get(WorkflowRecoveryCounter, outbox_id).failure_count == len(failures)
        assert all(event.run_id != run_id for event in pending_workflow_events(db))
    advance_pending(factory, settings)
    assert progress(client, episode)["error_code"] == "WORKFLOW_RECOVERY_LIMIT"
    producer = client.get(f"/api/v1/runs/{run_id}/producer").json()
    assert producer["pending_command"]["error_code"] == "WORKFLOW_RECOVERY_LIMIT"
    assert producer["can_recover"]
    recovered = client.post(
        f"/api/v1/runs/{run_id}/producer/commands",
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "action": "RECOVER",
            "expected_revision": revision,
            "command_id": producer["pending_command"]["command_id"],
        },
    )
    assert recovered.status_code == 202, recovered.text
    with factory() as db:
        assert db.get(WorkflowRecoveryCounter, outbox_id) is None
        assert [event.id for event in pending_workflow_events(db) if event.run_id == run_id] == [
            outbox_id
        ]


def test_scheduler_checks_art_and_storyboard_then_cancels_original_child(
    client, configured_episode, settings, model_service, monkeypatch
):
    episode = configured_episode
    reference_set(client, episode)
    content = legacy_script()
    content["scenes"][0]["dialogue"] = []
    script = save_script(client, episode, content=content)
    settings.model_gateway_policy = GatewayPolicy(text_maximum_cost="1", pricing_version="test:1")
    tasks = [
        {
            "task_key": "art",
            "depends_on": [],
            "reason": "需要确定雨夜场景与角色的风格",
            "task": {
                "kind": "art_director",
                "script_version_id": script["id"],
                "task": {
                    "visual_goal": "明确雨夜场景与角色的视觉风格",
                    "minimum_reference_images": 0,
                },
            },
        },
        {
            "task_key": "board",
            "depends_on": ["art"],
            "reason": "按定稿剧本与美术设计制作分镜",
            "task": {"kind": "director", "script_version_id": script["id"]},
        },
    ]
    queued = create_short_drama(client, episode, script)
    assert queued.status_code == 202, queued.text
    root = UUID(queued.json()["run_id"])
    project = UUID(episode["project_id"])
    factory = client.app.state.session_factory

    def revision():
        with factory() as db:
            return db.get(WorkflowRun, root).revision

    initial = revision()
    with factory.begin() as db:
        events = [event for event in pending_workflow_events(db) if event.run_id == root]
        assert len(events) == 1 and events[0].revision == initial
        events[0].published_at = utcnow()
    with factory.begin() as db:
        assert all(event.run_id != root for event in pending_workflow_events(db))
        command = db.get(WorkflowRun, root)
        assert command.status == "RUNNING"
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.run_id == root))
        event.published_at = utcnow() - timedelta(seconds=40)
    with factory.begin() as db:
        assert any(event.run_id == root for event in pending_workflow_events(db))

    balance_requests = []

    def insufficient_balance(request):
        balance_requests.append(request.content)
        return httpx.Response(402, json={"error": {"code": "insufficient_balance"}})

    model_service(insufficient_balance)
    execute_run(factory, settings, root, initial)
    advance_pending(factory, settings)
    blocked = progress(client, episode)
    assert blocked["status"] == "RUNNING" and blocked["revision"] == initial
    assert blocked["error_code"] == "ACCOUNT_BALANCE_INSUFFICIENT"
    assert blocked["can_retry_balance"]
    execute_run(factory, settings, root, initial)
    assert len(balance_requests) == 1
    with factory.begin() as db:
        assert all(event.run_id != root for event in pending_workflow_events(db))

    def recharge(revision):
        result = client.post(
            f"/api/v1/runs/{root}/short-drama/retry",
            headers={"Idempotency-Key": uuid4().hex},
            json={"expected_revision": revision},
        )
        assert result.status_code == 202, result.text

    recharge(initial)
    sent, errors = install_plan_model(model_service, cost="0.5", tasks=tasks)
    execute_run(factory, settings, root, initial)
    assert sent and not errors
    with factory.begin() as db:
        assert all(event.run_id != root for event in pending_workflow_events(db))
    execute_run(factory, settings, root, initial)
    assert len(sent) == 2
    advance_pending(factory, settings)
    state = progress(client, episode)
    assert state["phase"] == "ART", state
    delegated = revision()
    advance_pending(factory, settings)
    assert revision() == delegated
    execute_run(factory, settings, root, delegated)
    with factory.begin() as db:
        child_id = db.scalar(
            select(AgentRunLimit.run_id).where(AgentRunLimit.parent_run_id == root)
        )
        assert child_id is not None and db.get(WorkflowRun, child_id).status == "QUEUED"
        usage = action_usage(db, root)
        assert sum(action.kind == "MODEL" for action in usage["actions"]) == 3
        assert usage["pending_estimate"] == Decimal("2")
    denied = client.post(f"/api/v1/runs/{child_id}/cancel")
    assert denied.status_code == 409, denied.text

    with factory() as db:
        art_revision = db.get(WorkflowRun, child_id).revision
    model_service(insufficient_balance)
    execute_run(factory, settings, child_id, art_revision)
    advance_pending(factory, settings)
    blocked = progress(client, episode)
    assert blocked["phase"] == "ART" and blocked["status"] == "RUNNING"
    assert blocked["error_code"] == "ACCOUNT_BALANCE_INSUFFICIENT"
    assert blocked["can_retry_balance"]
    recharge(blocked["revision"])
    art_calls, art_errors, _ = install_direction_model(model_service)
    execute_run(factory, settings, child_id, art_revision)
    assert art_calls and not art_errors
    with factory() as db:
        child = db.get(WorkflowRun, child_id)
        assert child.status == "WAITING_REVIEW", child.error_code
        review = db.scalar(select(HumanReviewRequest).where(HumanReviewRequest.run_id == child_id))
        assert review is not None
        review_id = review.id
    manual_queue = client.get(f"/api/v1/projects/{project}/human-reviews").json()
    assert str(review_id) not in [review["id"] for review in manual_queue]
    read = client.get(f"/api/v1/human-reviews/{review_id}").json()
    assert read["automatic_production_id"] == str(root)
    assert read["blocking_code"] == "SHORT_DRAMA_AUTOMATIC"
    advance_pending(factory, settings)
    with factory() as db:
        child = db.get(WorkflowRun, child_id)
        assert child.status == "QUEUED", progress(client, episode)
        assert child.resume_json["authority"] == "SHORT_DRAMA"
        assert (
            db.scalar(
                select(HumanReviewDecision.id).where(HumanReviewDecision.request_id == review_id)
            )
            is None
        )
        art_revision = child.revision
    execute_run(factory, settings, child_id, art_revision)
    with factory() as db:
        art = db.get(WorkflowRun, child_id)
        assert art.status == "SUCCEEDED", art.error_code
    advance_pending(factory, settings)
    state = progress(client, episode)
    assert state["phase"] == "STORYBOARD", state
    delegated = revision()
    execute_run(factory, settings, root, delegated)
    with factory() as db:
        director_id = db.scalar(
            select(AgentRunLimit.run_id).where(
                AgentRunLimit.parent_run_id == root, AgentRunLimit.run_id != child_id
            )
        )
        assert director_id is not None and db.get(WorkflowRun, director_id).status == "QUEUED"
        director_revision = db.get(WorkflowRun, director_id).revision
    _, director_errors, plan_errors = install_revision(model_service, monkeypatch, settings)
    execute_run(factory, settings, director_id, director_revision)
    with factory() as db:
        director = db.get(WorkflowRun, director_id)
        assert director.status == "WAITING_REVIEW", (
            director.error_code,
            director_errors,
            plan_errors,
        )
        board_id = director.pending_review["version_id"]
    advance_pending(factory, settings)
    with factory() as db:
        director = db.get(WorkflowRun, director_id)
        assert director.status == "QUEUED", progress(client, episode)
        assert director.resume_json["authority"] == "SHORT_DRAMA"
        assert db.get(Storyboard, UUID(board_id)).status == "APPROVED"
    assert progress(client, episode)["storyboard_id"] == board_id
    stopped = client.post(
        f"/api/v1/runs/{root}/producer/commands",
        headers={"Idempotency-Key": uuid4().hex},
        json={"action": "CANCEL", "expected_revision": delegated},
    )
    assert stopped.status_code == 202, stopped.text
    assert progress(client, episode)["status"] == "CANCELLED"
    with factory() as db:
        child = db.get(WorkflowRun, director_id)
        assert child.status == child.agent_state_json["status"] == "CANCELLED"
    assert create_short_drama(client, episode, script).status_code == 202


def test_scheduler_delivers_only_after_exact_formal_render(
    client, episode, settings, timeline_case
):
    body, _ = timeline_case
    request = TimelineCreate.model_validate(body)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        script = {"id": str(db.get(Storyboard, request.storyboard_id).script_version_id)}
        db.get(Project, UUID(episode["project_id"])).output_profile = {
            "width": 256,
            "height": 256,
            "fps": 24,
        }
    voices = approve_recordings(client, episode, script)
    root, _ = authorized_run(client, episode, request.storyboard_id, generated_rights=True)
    bind_delivery_visuals(client, root, request.selected_shots)
    with factory.begin() as db:
        run = db.get(WorkflowRun, root)
        render = queue_short_drama_delivery(
            db,
            settings,
            run,
            request.storyboard_id,
            request.selected_shots,
            voice_asset_versions=voices,
        )
        set_progress(db, run, "RENDERING", storyboard_id=str(request.storyboard_id))
    advance_pending(factory, settings)
    assert progress(client, episode)["phase"] == "RENDERING"
    assert "delivery_id" not in progress(client, episode)
    assert execute_render(factory, settings, render.id) == "SUCCEEDED"
    advance_pending(factory, settings)
    result = progress(client, episode)
    assert (result["status"], result["phase"], result["render_progress"]) == (
        "SUCCEEDED",
        "DELIVERED",
        100,
    )
    with factory() as db:
        delivery = db.get(RenderArtifact, UUID(result["delivery_id"]))
        assert delivery.job_id == render.id and delivery.manifest_id == render.manifest_id
    advance_pending(factory, settings)
    assert progress(client, episode) == result
