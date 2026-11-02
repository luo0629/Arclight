"""CTX-01: real domain inputs, immutable snapshots and bounded provider consumption."""

import copy
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_api_workflow import advance, review, start
from test_collaboration import create_episode, create_project, register
from test_script_rewrite import body as rewrite_body
from test_script_rewrite import source as rewrite_source
from test_script_rewrite import start as start_rewrite
from test_story_bible import create as entry
from test_text_contract_v2 import make_asset, save_script
from test_visual_bible import edit, lock

from arclight.agents.contracts import ScriptContent
from arclight.application import context_snapshots
from arclight.application.commands import start_run
from arclight.core.errors import DomainError
from arclight.domain.context import TASK_CONTRACTS, ContextLimits
from arclight.persistence.models import (
    DependencyManifest,
    OutboxEvent,
    Project,
    VersionBinding,
    VisualAsset,
    WorkflowRun,
)
from arclight.providers.text import MockTextProvider, OpenAICompatibleTextProvider


def preview(client, episode, task="script", **values):
    return client.post(
        f"/api/v1/projects/{episode['project_id']}/contexts/preview",
        json={"task": task, "episode_id": episode["id"], **values},
    )


def contexts(client, run_id):
    response = client.get(f"/api/v1/runs/{run_id}/contexts")
    assert response.status_code == 200, response.text
    return response.json()


def visual_setup(client, episode):
    project = {"id": episode["project_id"]}
    assert edit(client, project, art_style="水彩", prohibited="禁止血腥").status_code == 201
    assert lock(client, project, 1, ["art_style", "prohibited"]).status_code == 201
    return project


def approved_board(client, episode, script):
    content = MockTextProvider().storyboard(
        ScriptContent.model_validate(script["content_json"]), 30000
    )
    base = f"/api/v1/episodes/{episode['id']}"
    response = client.post(
        base + "/storyboards",
        json={
            "expected_version_no": 0,
            "script_version_id": script["id"],
            "content": content.model_dump(mode="json"),
        },
    )
    assert response.status_code == 201, response.text
    board = response.json()
    review = client.post(base + f"/storyboards/{board['id']}/review", json={"decision": "APPROVE"})
    assert review.status_code == 200, review.text
    shots = client.get(base + "/shots", params={"storyboard_id": board["id"]}).json()
    return board, shots


@pytest.mark.parametrize("task", list(TASK_CONTRACTS))
def test_each_task_contract_reads_real_domain_data(client, episode, task):
    visual_setup(client, episode)
    entry(client, episode, rules=["不可复活"])
    script = save_script(client, episode)
    _, shots = approved_board(client, episode, script)
    values = {}
    contract = TASK_CONTRACTS[task]
    if contract.script:
        values["script_version_id"] = script["id"]
    if contract.shot:
        values["shot_id"] = shots[2]["id"]
    if task == "rewrite":
        values["rewrite_target"] = {
            "scope": "scene",
            "scene_id": script["content_json"]["scenes"][0]["scene_id"],
        }
    if task == "review":
        with client.app.state.session_factory.begin() as db:
            candidate = make_asset(db, episode["project_id"], "SHOT_IMAGE")
        values["candidate_version_id"] = candidate["asset_version_id"]
    response = preview(client, episode, task, **values)
    assert response.status_code == 200, response.text
    value = response.json()
    required = value["required"]
    assert value["task"] == task and value["builder_version"] == "context/1"
    assert required["episode"]["id"] == episode["id"]
    assert required["constraints"]["allowed_fields"] == list(contract.editable)
    assert required["constraints"]["budget"]["project_limit"] == "0.00"
    assert ("style" in required["global"]) == contract.style
    assert ("story" in required) == contract.entries
    assert ("assets" in required) == contract.assets
    if contract.style:
        assert required["constraints"]["locked_visual_fields"] == {
            "art_style": "水彩",
            "prohibited": "禁止血腥",
        }
    if contract.shot:
        assert required["local"]["shot"]["id"] == shots[2]["id"]
        assert [row["id"] for row in required["local"]["neighbors"]] == [
            shots[1]["id"],
            shots[3]["id"],
        ]
        assert "script" not in required["local"]
    if task == "rewrite":
        assert required["constraints"]["rewrite_target"] == values["rewrite_target"]
        assert "script" not in required["local"]


def test_local_tasks_omit_unrelated_entries_and_capture_exact_locked_assets(client, episode):
    entry(client, episode, name="世界", rules=["禁止瞬移"])
    unrelated = entry(client, episode, "CHARACTER", name="无关人物", description="不能泄入镜头")
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, episode["project_id"], "CHARACTER")
        asset = db.get(VisualAsset, UUID(reference["asset_id"]))
        asset.status = "LOCKED"
        asset.character_profile = {"fixed_features": ["左眼有痣"], "variable_features": []}
    script = save_script(
        client,
        episode,
        {
            "title": "镜头",
            "summary": "拾信",
            "characters": ["主角"],
            "character_assets": [{"character": "主角", "asset": reference}],
            "scenes": [{"scene_no": 1, "title": "街角", "action": "拾信"}],
        },
    )
    _, shots = approved_board(client, episode, script)
    response = preview(client, episode, "image", shot_id=shots[0]["id"])
    assert response.status_code == 200, response.text
    value = response.json()["required"]
    assert unrelated["id"] not in {row["id"] for row in value["story"]["entries"]}
    assert value["constraints"]["locked_assets"] == [reference["asset_version_id"]]
    exact = next(
        row for row in value["assets"] if row["asset_version_id"] == reference["asset_version_id"]
    )
    assert exact["profile"]["fixed_features"] == ["左眼有痣"]
    assert "object_key" not in json.dumps(value)


def test_profiles_resolve_only_chosen_exact_references_with_shared_asset_limit(client, episode):
    from arclight.persistence.models import AssetVersion

    with client.app.state.session_factory.begin() as db:
        character = make_asset(db, episode["project_id"], "CHARACTER")
        standard = make_asset(db, episode["project_id"], "SHOT_IMAGE")
        history = make_asset(db, episode["project_id"], "SHOT_IMAGE")
        standard_id, history_id = str(uuid4()), str(uuid4())
        db.get(VisualAsset, UUID(character["asset_id"])).character_profile = {
            "fixed_features": ["黑发"],
            "standard_image_id": standard_id,
            "images": [
                {"id": standard_id, "kind": "INITIAL", "source": standard},
                {"id": history_id, "kind": "ANGLE", "source": history},
            ],
        }
    response = preview(client, episode, asset_version_ids=[character["asset_version_id"]])
    assert response.status_code == 200, response.text
    assets = response.json()["required"]["assets"]
    assert {row["asset_version_id"] for row in assets} == {
        character["asset_version_id"],
        standard["asset_version_id"],
    }
    assert history["asset_version_id"] not in json.dumps(assets)
    assert (
        preview(
            client,
            episode,
            asset_version_ids=[character["asset_version_id"]],
            limits={"max_assets": 1},
        ).status_code
        == 422
    )
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(standard["asset_version_id"])).stale = True
    assert (
        preview(client, episode, asset_version_ids=[character["asset_version_id"]]).status_code
        == 409
    )


@pytest.mark.parametrize(
    "values,code",
    [
        ({"task": "image"}, "CONTEXT_INPUT_MISSING"),
        ({"task": "storyboard"}, "CONTEXT_INPUT_MISSING"),
        ({"task": "analyze", "asset_version_ids": [str(uuid4())]}, "CONTEXT_SCOPE_NOT_ALLOWED"),
        ({"task": "script", "script_version_id": str(uuid4())}, "CONTEXT_SCOPE_NOT_ALLOWED"),
        ({"task": "script", "story_entry_ids": [str(uuid4())]}, "NOT_FOUND"),
    ],
)
def test_missing_and_forbidden_scopes_are_deterministic(client, episode, values, code):
    values = dict(values)
    task = values.pop("task")
    response = preview(client, episode, task, **values)
    assert response.status_code in {404, 422}, response.text
    assert response.json()["error"]["code"] == code


def test_limits_never_trim_locked_or_required_constraints_and_start_is_atomic(client, episode):
    visual_setup(client, episode)
    entry(client, episode, name="一", rules=["不能复活"])
    entry(client, episode, "CHARACTER", name="二")
    response = preview(client, episode, limits={"max_story_entries": 1})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTEXT_TOO_LARGE"
    assert preview(client, episode, limits={"max_bytes": 1024}).status_code == 422
    assert preview(client, episode, limits={"max_bytes": 524289}).status_code == 422
    client.app.state.settings.context_limits = ContextLimits(max_bytes=1024)
    response = start(client, episode)
    assert response.status_code == 422, response.text
    with client.app.state.session_factory() as db:
        for model in (WorkflowRun, VersionBinding, DependencyManifest, OutboxEvent):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_service_limits_cannot_be_increased_and_lower_limits_are_frozen(client, episode):
    client.app.state.settings.context_limits = ContextLimits(max_bytes=8192, neighbor_radius=0)
    response = preview(client, episode, limits={"max_bytes": 524288, "neighbor_radius": 2})
    assert response.status_code == 200, response.text
    assert response.json()["limits"]["max_bytes"] == 8192
    assert response.json()["limits"]["neighbor_radius"] == 0


def test_startup_and_retry_keep_old_context_after_domain_and_policy_updates(
    client, episode, settings, monkeypatch
):
    visual_setup(client, episode)
    entry(client, episode, rules=["原始世界规则"])
    run_id = start(client, episode).json()["run_id"]
    old = contexts(client, run_id)
    assert set(old["contexts"]) == {"script", "storyboard"}
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(episode["project_id"]))
        project.output_profile = {"width": 500, "height": 500, "fps": 12}
        project.story_settings = {"content_restrictions": "新限制"}
        project.budget_limit = 99
    client.app.state.settings.context_limits = ContextLimits(max_bytes=1024)
    assert contexts(client, run_id) == old
    calls = []
    original = MockTextProvider.script

    def fail_once(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("controlled failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MockTextProvider, "script", fail_once)
    assert advance(client, settings, run_id)["status"] == "FAILED"
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
    assert contexts(client, run_id) == old
    with client.app.state.session_factory() as db:
        manifests = db.scalars(select(DependencyManifest)).all()
        assert (
            sum(row.snapshot_json["context"]["origin"] == "context_builder" for row in manifests)
            == 2
        )
        assert (
            sum(
                row.snapshot_json["context"]["origin"] == "context_consumption" for row in manifests
            )
            == 1
        )


def test_real_text_adapter_consumes_frozen_context_and_refuses_oversized_call(
    client, episode, settings, monkeypatch
):
    visual_setup(client, episode)
    run_id = start(client, episode).json()["run_id"]
    value = contexts(client, run_id)["contexts"]["script"]
    calls = []
    result = MockTextProvider().script("story", {}, 30000)

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": result.model_dump_json()}}]}
        )

    original = httpx.Client
    monkeypatch.setattr(
        "arclight.providers.text.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    provider = OpenAICompatibleTextProvider(settings, {"script": value})
    provider.script(
        "story",
        {},
        30000,
        outline_context={
            "source_snapshot": {
                "source_revision": {
                    "source_id": "stable-source",
                    "sha256": "a" * 64,
                    "object_ref": {"backend": "s3", "key": "private/source-locator"},
                }
            }
        },
    )
    sent = json.loads(calls[0]["messages"][1]["content"])
    assert sent["task_context"] == value
    assert sent["task_input"]["story"] == "story"
    assert "private/source-locator" not in json.dumps(sent)
    assert (
        sent["task_input"]["outline_context"]["source_snapshot"]["source_revision"]["source_id"]
        == "stable-source"
    )
    with pytest.raises(DomainError, match="不安全"):
        provider.script("Bearer should-never-be-sent", {}, 30000)
    too_small = copy.deepcopy(value)
    too_small["limits"]["max_bytes"] = 1024
    with pytest.raises(DomainError, match="必需上下文"):
        OpenAICompatibleTextProvider(settings, {"script": too_small}).script("story", {}, 30000)
    assert len(calls) == 1


def test_preview_read_permissions_and_cross_project_do_not_leak(client):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    run_id = start(client, episode).json()["run_id"]
    viewer = register(client, "context-viewer@example.com")
    viewer_auth = client.headers["Authorization"]
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/members",
            json={"email": viewer["user"]["email"], "role": "viewer"},
        ).status_code
        == 201
    )
    client.headers["Authorization"] = viewer_auth
    assert preview(client, episode).status_code == 200
    assert client.get(f"/api/v1/runs/{run_id}/contexts").status_code == 200
    assert start(client, episode, key="viewer-denied").status_code == 403
    other = create_project(client)
    assert (
        client.post(
            f"/api/v1/projects/{other['id']}/contexts/preview",
            json={"task": "script", "episode_id": episode["id"]},
        ).status_code
        == 404
    )
    register(client, "context-outsider@example.com")
    assert preview(client, episode).status_code == 404
    assert client.get(f"/api/v1/runs/{run_id}/contexts").status_code == 404


def test_exact_assets_scripts_and_shots_cannot_cross_projects(client, episode):
    other = create_episode(client, create_project(client))
    script = save_script(client, other)
    _, shots = approved_board(client, other, script)
    with client.app.state.session_factory.begin() as db:
        asset = make_asset(db, other["project_id"], "CHARACTER")
    cases = [
        ("script", {"asset_version_ids": [asset["asset_version_id"]]}),
        ("storyboard", {"script_version_id": script["id"]}),
        ("image", {"shot_id": shots[0]["id"]}),
    ]
    for task, values in cases:
        response = preview(client, episode, task, **values)
        assert response.status_code == 404, response.text


@pytest.mark.parametrize("state", ["ARCHIVED", "REJECTED", "STALE"])
def test_unusable_asset_lifecycle_is_rejected(client, episode, state):
    with client.app.state.session_factory.begin() as db:
        asset = make_asset(db, episode["project_id"], "PROP")
        db.get(VisualAsset, UUID(asset["asset_id"])).status = state
    response = preview(client, episode, asset_version_ids=[asset["asset_version_id"]])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONTEXT_INPUT_NOT_USABLE"


def test_rewrite_and_other_text_entry_points_have_frozen_context(client, episode, settings):
    base = rewrite_source(client, episode)
    response = start_rewrite(client, episode, rewrite_body(base))
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    value = contexts(client, run_id)["contexts"]["rewrite"]
    assert value["required"]["constraints"]["rewrite_target"] == rewrite_body(base)["target"]
    assert len(value["required"]["local"]["selection"]["scenes"]) == 1
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"


@pytest.mark.parametrize("operation", ["script", "storyboard", "rewrite", "analyze", "outline"])
def test_every_existing_entry_point_consumes_its_frozen_context(
    client, episode, settings, monkeypatch, operation
):
    from arclight.providers.context import ContextTextProvider

    calls = []
    original = ContextTextProvider.check

    def consume(self, stage, value):
        calls.append((stage, copy.deepcopy(self.contexts[stage])))
        return original(self, stage, value)

    monkeypatch.setattr(ContextTextProvider, "check", consume)
    prefix = f"/api/v1/episodes/{episode['id']}"
    if operation == "script":
        response = start(client, episode)
    elif operation == "rewrite":
        source = rewrite_source(client, episode)
        response = start_rewrite(client, episode, rewrite_body(source))
    else:
        endpoint = {
            "storyboard": "storyboard-runs",
            "analyze": "story-analysis-runs",
            "outline": "story-outline-runs",
        }[operation]
        body = (
            {"script_version_id": save_script(client, episode)["id"]}
            if operation == "storyboard"
            else {"story": "主角找到信。"}
        )
        response = client.post(
            prefix + "/" + endpoint, json=body, headers={"Idempotency-Key": "context-consumption"}
        )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    frozen = contexts(client, run_id)["contexts"]
    settings.context_limits = ContextLimits(max_bytes=1024)
    state = advance(client, settings, run_id)
    assert state["status"] in {"WAITING_REVIEW", "SUCCEEDED"}, state
    assert calls == [(operation, frozen[operation])]
    if operation == "script":
        assert review(client, state).status_code == 202
        state = advance(client, settings, run_id)
        assert state["status"] == "WAITING_REVIEW", state
        assert calls[-1] == ("storyboard", frozen["storyboard"])


def test_history_is_bounded_related_and_excludes_other_projects(client, episode):
    from test_asset_selection import approved, current
    from test_assets import create
    from test_generation_attempts import fail
    from test_generation_attempts import start as submit
    from test_generation_jobs import capture_ok, payload

    from arclight.persistence.models import Review

    target = create(client, episode["project_id"])
    reference = approved(client, target)
    target = current(client, target)
    body = payload(target, episode_id=episode["id"])
    job = capture_ok(client, episode["project_id"], body)
    attempt = submit(client, job)
    fail(client, job, attempt)
    unrelated = capture_ok(client, episode["project_id"], payload())
    other_attempt = submit(client, unrelated)
    fail(client, unrelated, other_attempt)
    script = save_script(client, episode)
    other_project = create_project(client)
    other_episode = create_episode(client, other_project)
    other_run = start(client, other_episode).json()["run_id"]
    with client.app.state.session_factory.begin() as db:
        db.add(
            Review(
                run_id=UUID(other_run),
                resource_type="script",
                version_id=UUID(script["id"]),
                reviewer_id="service-owner",
                decision="REJECT",
                comment="foreign-project-feedback",
            )
        )
    response = preview(
        client, episode, "art", script_version_id=script["id"], asset_version_ids=[reference["id"]]
    )
    assert response.status_code == 200, response.text
    history = response.json()["history"]
    attempts = [row for row in history if row["kind"] == "FAILED_ATTEMPT"]
    assert [row["attempt_id"] for row in attempts] == [str(attempt.id)]
    serialized = json.dumps(history)
    assert "foreign-project-feedback" not in serialized
    assert "result_json" not in serialized and "provider_request_id" not in serialized
    assert (
        preview(
            client, episode, "art", script_version_id=script["id"], limits={"max_history": 0}
        ).json()["history"]
        == []
    )


def test_review_candidate_is_exact_and_can_be_unapproved_but_references_cannot(client, episode):
    from arclight.persistence.models import AssetVersion

    script = save_script(client, episode)
    _, shots = approved_board(client, episode, script)
    with client.app.state.session_factory.begin() as db:
        candidate = make_asset(db, episode["project_id"], "SHOT_IMAGE")
        db.get(AssetVersion, UUID(candidate["asset_version_id"])).status = "DRAFT"
    assert preview(client, episode, "review", shot_id=shots[0]["id"]).status_code == 422
    response = preview(
        client,
        episode,
        "review",
        shot_id=shots[0]["id"],
        candidate_version_id=candidate["asset_version_id"],
    )
    assert response.status_code == 200, response.text
    assert any(
        row["purpose"] == "REVIEW_CANDIDATE" for row in response.json()["required"]["assets"]
    )
    assert (
        preview(
            client,
            episode,
            "image",
            shot_id=shots[0]["id"],
            asset_version_ids=[candidate["asset_version_id"]],
        ).status_code
        == 409
    )
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(candidate["asset_version_id"])).stale = True
    assert (
        preview(
            client,
            episode,
            "review",
            shot_id=shots[0]["id"],
            candidate_version_id=candidate["asset_version_id"],
        ).status_code
        == 409
    )


def test_missing_snapshot_refuses_execution_and_legacy_never_reads_current(
    client, episode, settings, monkeypatch
):
    run_id = start(client, episode).json()["run_id"]
    original = context_snapshots.manifest_references
    monkeypatch.setattr(
        context_snapshots,
        "manifest_references",
        lambda *args: {"script": str(uuid4()), "storyboard": str(uuid4())},
    )
    assert advance(client, settings, run_id)["error_code"] == "CONTEXT_SNAPSHOT_INVALID"
    monkeypatch.setattr(context_snapshots, "manifest_references", original)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        monkeypatch.setattr(context_snapshots, "manifest_references", lambda *args: None)
        assert context_snapshots.read_contexts(db, UUID(episode["project_id"]), run) == {}


def test_postgres_context_capture_and_project_update_are_serialized(client, episode, settings):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_concurrency import require_postgres

    require_postgres(settings)
    captured = Event()
    release = Event()
    factory = client.app.state.session_factory

    def capture():
        with factory.begin() as db:
            run = start_run(
                db,
                UUID(episode["id"]),
                "service-owner",
                "story",
                "concurrent",
                "ctx-test",
                settings=settings,
            )
            captured.set()
            assert release.wait(10)
            return str(run.id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(capture)
        assert captured.wait(10)
        with factory.begin() as db:
            from sqlalchemy import text

            db.execute(text("SET LOCAL lock_timeout='150ms'"))
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError, match="lock timeout"):
                db.get(Project, UUID(episode["project_id"]), with_for_update=True)
        release.set()
        run_id = future.result(10)
    old = contexts(client, run_id)
    with factory.begin() as db:
        db.get(Project, UUID(episode["project_id"]), with_for_update=True).output_profile = {
            "width": 500
        }
    assert contexts(client, run_id) == old
