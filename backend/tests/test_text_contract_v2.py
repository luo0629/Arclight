"""TXT-09：合同、精确引用和旧内容兼容；仅隔离数据库与 Fake Provider。"""

import copy
import json
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from arclight.agents.contracts import AssetReference, ScriptContent
from arclight.application import runtime
from arclight.application.runtime import execute_run
from arclight.persistence.models import (
    AssetVersion,
    Project,
    Review,
    Script,
    Storyboard,
    VisualAsset,
    WorkflowRun,
)
from arclight.providers.text import MockTextProvider, OpenAICompatibleTextProvider


def legacy_script():
    return {
        "title": "雨夜",
        "summary": "主角在雨夜找到旧信",
        "characters": ["主角"],
        "scenes": [
            {
                "scene_no": 1,
                "title": "街道",
                "action": "主角驻足",
                "dialogue": [
                    {"speaker": "主角", "text": "我回来了。"},
                    {"speaker": "旁白", "text": "雨停了。"},
                ],
            }
        ],
    }


def save_script(client, episode, content=None, approve=True):
    path = f"/api/v1/episodes/{episode['id']}"
    result = client.post(
        path + "/scripts", json={"expected_version_no": 0, "content": content or legacy_script()}
    )
    assert result.status_code == 201, result.text
    row = result.json()
    if approve:
        response = client.post(path + f"/scripts/{row['id']}/review", json={"decision": "APPROVE"})
        assert response.status_code == 200, response.text
    return row


def make_asset(db, project_id, kind, **changes):
    asset = VisualAsset(project_id=UUID(project_id), asset_type=kind, name=kind, status="APPROVED")
    db.add(asset)
    db.flush()
    version = AssetVersion(
        asset_id=asset.id,
        version_no=1,
        object_key="fixture/only",
        checksum="a" * 64,
        created_by="service-owner",
        status="APPROVED",
        **changes,
    )
    db.add(version)
    db.flush()
    return {"asset_id": str(asset.id), "asset_version_id": str(version.id)}


@pytest.fixture
def assets(client, episode):
    with client.app.state.session_factory.begin() as db:
        return {
            kind: make_asset(db, episode["project_id"], kind)
            for kind in ["CHARACTER", "SCENE", "LOCATION", "PROP", "COSTUME", "IMAGE"]
        }


def board_content(script):
    content = (
        MockTextProvider()
        .storyboard(ScriptContent.model_validate(script["content_json"]), 30000)
        .model_dump()
    )
    content["shots"] = content["shots"][:1]
    return content


def save_board(client, episode, script, content):
    return client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards",
        json={
            "expected_version_no": 0,
            "script_version_id": script["id"],
            "content": content,
        },
    )


def test_legacy_conversion_is_deterministic_non_mutating_and_preserves_identity():
    old = legacy_script()
    before = copy.deepcopy(old)
    first = ScriptContent.model_validate(old).model_dump()
    assert ScriptContent.model_validate(old).model_dump() == first
    assert old == before and "schema_version" not in old
    lines = first["scenes"][0]["dialogue"]
    assert lines[0]["line_type"] == "dialogue" and lines[1]["line_type"] == "narration"
    original_narration_id = lines[1]["line_id"]
    first["scenes"][0]["dialogue"].reverse()
    first["scenes"][0]["dialogue"][0]["text"] = "新的旁白"
    updated = ScriptContent.model_validate(first)
    assert updated.scenes[0].dialogue[0].line_id == original_narration_id
    assert updated.scenes[0].scene_id == first["scenes"][0]["scene_id"]


@pytest.mark.parametrize(
    "field,value", [("scenes", None), ("scenes", 12), ("scenes", []), ("schema_version", "9.0")]
)
def test_bad_legacy_shapes_are_validation_errors(field, value):
    data = legacy_script()
    data[field] = value
    with pytest.raises(ValidationError):
        ScriptContent.model_validate(data)


@pytest.mark.parametrize(
    "mutation", ["duplicate_line", "duplicate_scene", "wrong_type", "invalid_id", "unknown_field"]
)
def test_v2_rejects_invalid_identity_and_type(mutation):
    data = ScriptContent.model_validate(legacy_script()).model_dump()
    scene = data["scenes"][0]
    if mutation == "duplicate_line":
        scene["dialogue"][1]["line_id"] = scene["dialogue"][0]["line_id"]
    elif mutation == "duplicate_scene":
        data["scenes"].append({**scene, "scene_no": 2, "dialogue": []})
    elif mutation == "wrong_type":
        scene["dialogue"][0]["line_type"] = "narration"
    elif mutation == "invalid_id":
        scene["dialogue"][0]["line_id"] = "name-is-not-an-id"
    else:
        scene["dialogue"][0]["surprise"] = True
    with pytest.raises(ValidationError):
        ScriptContent.model_validate(data)


def test_old_rows_read_only_normalization_and_exact_dialogue_endpoint(client, episode):
    old = legacy_script()
    with client.app.state.session_factory.begin() as db:
        row = Script(
            episode_id=UUID(episode["id"]), version_no=1, content_json=old, status="APPROVED"
        )
        db.add(row)
        db.flush()
        version_id = row.id
    path = f"/api/v1/episodes/{episode['id']}"
    read = client.get(path + "/scripts").json()[0]
    again = client.get(path + "/scripts").json()[0]
    assert read["content_json"] == again["content_json"]
    lines = client.get(
        path + "/dialogue-lines",
        params={"script_version_id": str(version_id), "offset": 1, "limit": 1},
    )
    assert lines.status_code == 200 and len(lines.json()) == 1
    assert lines.json()[0]["line_id"] == read["content_json"]["scenes"][0]["dialogue"][1]["line_id"]
    assert lines.json()[0]["script_version_id"] == str(version_id)
    assert client.get(path + "/dialogue-lines").status_code == 422
    assert (
        client.get(path + "/dialogue-lines", params={"script_version_id": str(uuid4())}).status_code
        == 404
    )
    changed = copy.deepcopy(read["content_json"])
    changed["scenes"][0]["dialogue"][0]["text"] = "另一句对白"
    saved = client.post(
        path + "/scripts",
        json={"expected_version_no": 1, "base_version_id": str(version_id), "content": changed},
    )
    assert saved.status_code == 201, saved.text
    assert (
        saved.json()["content_json"]["scenes"][0]["dialogue"][0]["line_id"]
        == read["content_json"]["scenes"][0]["dialogue"][0]["line_id"]
    )
    with client.app.state.session_factory() as db:
        assert db.get(Script, version_id).content_json == old
    other = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "另一集"},
    ).json()
    assert (
        client.get(
            f"/api/v1/episodes/{other['id']}/dialogue-lines",
            params={"script_version_id": str(version_id)},
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "problem",
    ["missing", "cross_project", "wrong_type", "wrong_identity", "stale", "draft", "archived"],
)
def test_script_asset_reference_rejected_atomically(client, episode, assets, problem):
    reference = assets["CHARACTER"].copy()
    with client.app.state.session_factory.begin() as db:
        if problem == "missing":
            reference["asset_version_id"] = str(uuid4())
        elif problem == "cross_project":
            project = Project(
                name="其他项目", owner_id="unrelated", style_guide={}, output_profile={}
            )
            db.add(project)
            db.flush()
            reference = make_asset(db, str(project.id), "CHARACTER")
        elif problem == "wrong_type":
            reference = assets["PROP"]
        elif problem == "wrong_identity":
            reference["asset_id"] = assets["PROP"]["asset_id"]
        elif problem == "stale":
            db.get(AssetVersion, UUID(reference["asset_version_id"])).stale = True
        elif problem == "draft":
            db.get(AssetVersion, UUID(reference["asset_version_id"])).status = "DRAFT"
        else:
            db.get(VisualAsset, UUID(reference["asset_id"])).status = "ARCHIVED"
    data = legacy_script()
    data["character_assets"] = [{"character": "主角", "asset": reference}]
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/scripts",
        json={"expected_version_no": 0, "content": data},
    )
    assert response.status_code == 422, response.text
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Script)) == 0


def bound_script(assets):
    data = legacy_script()
    data["character_assets"] = [{"character": "主角", "asset": assets["CHARACTER"]}]
    data["scenes"][0].update(
        scene_asset=assets["SCENE"], location_asset=assets["LOCATION"], props=[assets["PROP"]]
    )
    return data


def test_all_precise_references_and_metadata_round_trip(client, episode, assets):
    script = save_script(client, episode, bound_script(assets))
    content = board_content(script)
    shot = content["shots"][0]
    shot.update(
        composition="右三分之一",
        sound_effects=["雨声"],
        transition_out="dissolve",
        notes="保留视线方向",
        selected_visual_asset=assets["IMAGE"],
    )
    shot["character_details"][0].update(
        costume=assets["COSTUME"], action="回头", expression="警惕", gaze="左后方"
    )
    with client.app.state.session_factory.begin() as db:
        run = WorkflowRun(
            episode_id=UUID(episode["id"]),
            status="SUCCEEDED",
            trace_id="fixture",
            idempotency_key="fixture",
            request_fingerprint="f" * 64,
            input_json={},
        )
        db.add(run)
        db.flush()
        review = Review(
            run_id=run.id,
            resource_type="asset_version",
            version_id=UUID(assets["IMAGE"]["asset_version_id"]),
            reviewer_id="service-owner",
            decision="APPROVE",
        )
        db.add(review)
        db.flush()
        shot["quality_review"] = {
            "review_id": str(review.id),
            "asset_version_id": assets["IMAGE"]["asset_version_id"],
        }
    response = save_board(client, episode, script, content)
    assert response.status_code == 201, response.text
    assert response.json()["content_json"] == content
    prefix = f"/api/v1/episodes/{episode['id']}"
    saved_shots = client.get(
        prefix + "/shots", params={"storyboard_id": response.json()["id"]}
    ).json()
    assert saved_shots[0]["content_json"] == shot
    changed = copy.deepcopy(content)
    changed["shots"][0]["notes"] = "新的备注"
    second = client.post(
        prefix + "/storyboards",
        json={
            "expected_version_no": 1,
            "base_version_id": response.json()["id"],
            "script_version_id": script["id"],
            "content": changed,
        },
    )
    assert second.status_code == 201, second.text
    assert client.get(prefix + "/storyboards").json()[1]["content_json"] == content


@pytest.mark.parametrize(
    "problem",
    [
        "line",
        "scene",
        "speaker",
        "binding",
        "missing_binding",
        "costume",
        "selection",
        "quality_missing",
        "quality_version",
        "quality_text_review",
    ],
)
def test_storyboard_invalid_references_cannot_save(client, episode, assets, problem):
    script = save_script(client, episode, bound_script(assets))
    content = board_content(script)
    shot = content["shots"][0]
    if problem == "line":
        shot["dialogue_line_ids"] = [str(uuid4())]
    elif problem == "scene":
        shot["scene_id"] = str(uuid4())
    elif problem == "speaker":
        shot["characters"] = []
        shot["character_details"] = []
    elif problem == "binding":
        shot["character_details"][0]["asset"] = None
    elif problem == "missing_binding":
        shot["character_details"] = []
    elif problem == "costume":
        shot["character_details"][0]["costume"] = assets["PROP"]
    elif problem == "selection":
        shot["selected_visual_asset"] = assets["CHARACTER"]
    else:
        shot["selected_visual_asset"] = assets["IMAGE"]
        shot["quality_review"] = {
            "review_id": str(uuid4()),
            "asset_version_id": assets["IMAGE"]["asset_version_id"],
        }
        if problem == "quality_version":
            shot["quality_review"]["asset_version_id"] = assets["PROP"]["asset_version_id"]
        if problem == "quality_text_review":
            with client.app.state.session_factory.begin() as db:
                run = WorkflowRun(
                    episode_id=UUID(episode["id"]),
                    status="SUCCEEDED",
                    trace_id="x",
                    idempotency_key="x",
                    request_fingerprint="x" * 64,
                    input_json={},
                )
                db.add(run)
                db.flush()
                review = Review(
                    run_id=run.id,
                    resource_type="script",
                    version_id=UUID(assets["IMAGE"]["asset_version_id"]),
                    reviewer_id="service-owner",
                    decision="APPROVE",
                )
                db.add(review)
                db.flush()
                shot["quality_review"]["review_id"] = str(review.id)
    response = save_board(client, episode, script, content)
    assert response.status_code == 422, response.text
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0


def test_fake_provider_invalid_asset_cannot_persist_and_explicit_retry_recovers(
    client, episode, settings, monkeypatch
):
    class Broken(MockTextProvider):
        def script(self, story, style):
            data = legacy_script()
            data["character_assets"] = [
                {
                    "character": "主角",
                    "asset": {"asset_id": str(uuid4()), "asset_version_id": str(uuid4())},
                }
            ]
            return ScriptContent.model_validate(data)

    monkeypatch.setattr(runtime, "build_provider", lambda _: Broken())
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        json={"story": "测试"},
        headers={"Idempotency-Key": "invalid-ref"},
    ).json()
    run_id = response["run_id"]
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "FAILED"
    assert client.get(f"/api/v1/episodes/{episode['id']}/scripts").json() == []
    retry = client.post(f"/api/v1/runs/{run_id}/retry").json()
    monkeypatch.setattr(runtime, "build_provider", lambda _: MockTextProvider())
    execute_run(client.app.state.session_factory, settings, UUID(run_id), retry["revision"])
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "WAITING_REVIEW"


def test_stale_asset_blocks_manual_approval_and_director_start(client, episode, assets):
    script = save_script(client, episode, bound_script(assets), approve=False)
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(assets["CHARACTER"]["asset_version_id"])).stale = True
    prefix = f"/api/v1/episodes/{episode['id']}"
    assert (
        client.post(
            prefix + f"/scripts/{script['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 422
    )
    with client.app.state.session_factory.begin() as db:
        db.get(Script, UUID(script["id"])).status = "APPROVED"
    assert (
        client.post(
            prefix + "/storyboard-runs",
            json={"script_version_id": script["id"]},
            headers={"Idempotency-Key": "stale-start"},
        ).status_code
        == 422
    )


def test_http_provider_uses_v2_schema_and_keeps_input_in_data(settings, monkeypatch):
    requests = []
    script = ScriptContent.model_validate(legacy_script())
    original_client = httpx.Client

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": script.model_dump_json()}}]}
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = OpenAICompatibleTextProvider(settings).script("只作为素材", {})
    assert result == script
    data = requests[0]
    assert "只作为素材" not in data["messages"][0]["content"]
    assert json.loads(data["messages"][1]["content"])["story"] == "只作为素材"
    schema = data["response_format"]["json_schema"]["schema"]
    assert "line_id" in schema["$defs"]["Dialogue"]["properties"]
    assert "character_assets" in schema["properties"]


@pytest.mark.parametrize("problem", ["provider_asset", "source_stale", "foreign_line"])
def test_director_rechecks_exact_source_and_rejects_bad_output(
    client, episode, assets, settings, monkeypatch, problem
):
    script = save_script(client, episode, bound_script(assets))

    class Broken(MockTextProvider):
        def storyboard(self, script, target_duration_ms):
            content = super().storyboard(script, target_duration_ms)
            if problem == "provider_asset":
                content.shots[0].props = [
                    AssetReference(asset_id=str(uuid4()), asset_version_id=str(uuid4()))
                ]
            elif problem == "foreign_line":
                content.shots[0].dialogue_line_ids = [str(uuid4())]
            return content

    monkeypatch.setattr(runtime, "build_provider", lambda _: Broken())
    prefix = f"/api/v1/episodes/{episode['id']}"
    result = client.post(
        prefix + "/storyboard-runs",
        json={"script_version_id": script["id"]},
        headers={"Idempotency-Key": "director-invalid"},
    )
    assert result.status_code == 202, result.text
    run_id = result.json()["run_id"]
    if problem == "source_stale":
        with client.app.state.session_factory.begin() as db:
            db.get(AssetVersion, UUID(assets["CHARACTER"]["asset_version_id"])).stale = True
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "FAILED"
    assert client.get(prefix + "/storyboards").json() == []


def test_workflow_approval_rechecks_references_without_consuming_review(
    client, episode, assets, settings, monkeypatch
):
    class Bound(MockTextProvider):
        def script(self, story, style):
            return ScriptContent.model_validate(bound_script(assets))

    monkeypatch.setattr(runtime, "build_provider", lambda _: Bound())
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        json={"story": "测试"},
        headers={"Idempotency-Key": "workflow-stale"},
    )
    run_id = response.json()["run_id"]
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    for resource in ("script", "storyboard"):
        run = client.get(f"/api/v1/runs/{run_id}").json()
        assert run["pending_review"]["resource_type"] == resource
        with client.app.state.session_factory.begin() as db:
            db.get(AssetVersion, UUID(assets["CHARACTER"]["asset_version_id"])).stale = True
        body = {
            **run["pending_review"],
            "decision": "APPROVE",
            "expected_revision": run["revision"],
        }
        blocked = client.post(f"/api/v1/runs/{run_id}/resume", json=body)
        assert blocked.status_code == 422, blocked.text
        unchanged = client.get(f"/api/v1/runs/{run_id}").json()
        assert unchanged["status"] == "WAITING_REVIEW" and unchanged["revision"] == run["revision"]
        with client.app.state.session_factory.begin() as db:
            reviews = db.scalars(select(Review).where(Review.run_id == UUID(run_id))).all()
            assert len(reviews) == (0 if resource == "script" else 1)
            db.get(AssetVersion, UUID(assets["CHARACTER"]["asset_version_id"])).stale = False
        approved = client.post(f"/api/v1/runs/{run_id}/resume", json=body)
        assert approved.status_code == 202, approved.text
        execute_run(
            client.app.state.session_factory, settings, UUID(run_id), approved.json()["revision"]
        )
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "SUCCEEDED"


@pytest.mark.parametrize("problem", ["wrong_project", "rejected"])
def test_quality_review_must_belong_to_project_and_be_approved(client, episode, assets, problem):
    from test_collaboration import create_episode, create_project

    script = save_script(client, episode, bound_script(assets))
    content = board_content(script)
    shot = content["shots"][0]
    shot["selected_visual_asset"] = assets["IMAGE"]
    review_episode = (
        create_episode(client, create_project(client)) if problem == "wrong_project" else episode
    )
    with client.app.state.session_factory.begin() as db:
        run = WorkflowRun(
            episode_id=UUID(review_episode["id"]),
            status="SUCCEEDED",
            trace_id="quality",
            idempotency_key="quality",
            request_fingerprint="f" * 64,
            input_json={},
        )
        db.add(run)
        db.flush()
        review = Review(
            run_id=run.id,
            resource_type="asset_version",
            version_id=UUID(assets["IMAGE"]["asset_version_id"]),
            reviewer_id="service-owner",
            decision="REJECT" if problem == "rejected" else "APPROVE",
        )
        db.add(review)
        db.flush()
        shot["quality_review"] = {
            "review_id": str(review.id),
            "asset_version_id": assets["IMAGE"]["asset_version_id"],
        }
    response = save_board(client, episode, script, content)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_QUALITY_REFERENCE"
