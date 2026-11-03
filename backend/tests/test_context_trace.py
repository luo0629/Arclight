"""CTX-02: immutable source/evidence histories and actual bounded consumption."""

import base64
import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import DBAPIError
from storage_helpers import clear_test_bucket
from test_api_workflow import advance, review, start
from test_asset_versions import image_bytes
from test_asset_versions import save as save_image
from test_asset_versions import state as image_state
from test_assets import create as create_asset
from test_collaboration import create_episode, create_project, register
from test_contexts import contexts, visual_setup
from test_dependency_manifests import capture
from test_story_bible import create as entry
from test_story_bible import replace
from test_story_outline import confirm, derive, generate
from test_story_source_revisions import revise, save_text
from test_text_contract_v2 import make_asset, save_script

from arclight.application import context_sources
from arclight.application.context_sources import public_startup
from arclight.domain.context import ContextLimits
from arclight.domain.versions import digest
from arclight.persistence.models import DependencyManifest, StoredFile, StoryEntry
from arclight.providers.context import ContextTextProvider
from arclight.providers.text import MockTextProvider, OpenAICompatibleTextProvider
from arclight.storage.objects import ObjectStorage, initialize
from arclight.workflows.checkpoints import open_checkpointer


def path(run_id, stage="script"):
    return f"/api/v1/runs/{run_id}/contexts/{stage}"


def trace(client, run_id, stage="script", through=None):
    params = {} if through is None else {"through_round": through}
    response = client.get(path(run_id, stage) + "/trace", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def evidence(client, run_id, stage="script", **values):
    return client.post(
        path(run_id, stage) + "/evidence",
        json={
            "creation_key": str(uuid4()),
            "expected_round": 0,
            **values,
        },
    )


def run(client, episode):
    response = start(client, episode)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def test_startup_provenance_is_safe_fixed_and_includes_every_selected_source(client, episode):
    visual_setup(client, episode)
    world = entry(client, episode, rules=["原始规则"])
    source = save_text(client, episode)
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        headers={"Idempotency-Key": "precise-source"},
        json={"source_id": source["id"]},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    frozen = trace(client, run_id)
    startup = frozen["startup"]
    assert startup["trace_version"] == "context-trace/1"
    assert startup["builder_version"] == "context/1"
    assert startup["startup_sha256"] == digest(startup["startup_input"])
    selected = startup["sources"]
    proof = next(item for item in selected if item["kind"] == "STORY_SOURCE")
    assert proof["source_id"] == source["id"]
    assert UUID(proof["revision_id"])
    assert proof["sha256"] == source["sha256"] and proof["version_no"] == 1
    assert any(item["kind"] == "VISUAL_BIBLE" for item in selected)
    assert any(item["kind"] == "STORY_ENTRY" and item["id"] == world["id"] for item in selected)
    assert replace(client, episode, world, rules=["新规则"]).status_code == 409
    with client.app.state.session_factory.begin() as db:
        live = db.get(StoryEntry, UUID(world["id"]))
        live.data_json = {**live.data_json, "rules": ["新规则"]}
        live.revision += 1
    save_text(client, episode, revise(client, episode, source, text="新版原稿"))
    assert trace(client, run_id) == frozen
    serialized = json.dumps(frozen)
    for private in ("object_key", "story-sources/", "_text_service", "raw_bytes", "content_base64"):
        assert private not in serialized
    assert public_startup({"story": "x", "_text_service": {"api_key": "secret"}}) == {"story": "x"}


def test_outline_and_analysis_provenance_and_exact_text_origin(client, episode, settings):
    source = save_text(client, episode)
    analyzed = client.post(
        f"/api/v1/episodes/{episode['id']}/story-analysis-runs",
        headers={"Idempotency-Key": "analysis"},
        json={"source_id": source["id"]},
    )
    analysis_run = analyzed.json()["run_id"]
    assert advance(client, settings, analysis_run)["status"] == "SUCCEEDED"
    analysis = client.get(f"/api/v1/episodes/{episode['id']}/story-analyses").json()[0]
    outline = generate(client, episode, settings, {"analysis_id": analysis["id"]})
    assert confirm(client, episode, outline).status_code == 200
    response = derive(client, episode, outline)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    sources = trace(client, run_id)["startup"]["sources"]
    kinds = {item["kind"] for item in sources}
    assert {"OUTLINE", "ANALYSIS", "STORY_SOURCE"} <= kinds
    assert next(item for item in sources if item["kind"] == "OUTLINE")["id"] == outline["id"]
    assert next(item for item in sources if item["kind"] == "ANALYSIS")["id"] == analysis["id"]
    assert trace(client, analysis_run, "analyze")["consumption"] is not None


def test_rounds_are_linked_replayable_and_old_revision_contents_never_change(client, episode):
    world = entry(client, episode, rules=["第一轮规则"])
    first_source = save_text(client, episode)
    run_id = run(client, episode)
    body = {
        "creation_key": str(uuid4()),
        "story_entry_ids": [world["id"]],
        "source_ids": [first_source["id"]],
    }
    first = evidence(client, run_id, **body)
    assert first.status_code == 201, first.text
    first = first.json()
    historic = trace(client, run_id, through=1)
    assert first["startup_manifest_id"] == historic["startup_manifest_id"]
    assert first["parent_manifest_id"] == first["startup_manifest_id"]
    assert first["stories"] == [{"source_id": first_source["id"], "text": first_source["text"]}]
    with client.app.state.session_factory.begin() as db:
        live = db.get(StoryEntry, UUID(world["id"]))
        live.data_json = {**live.data_json, "rules": ["第二轮规则"]}
        live.revision += 1
    second_source = save_text(client, episode, revise(client, episode, first_source, text="第二稿"))
    second = evidence(
        client,
        run_id,
        expected_round=1,
        story_entry_ids=[world["id"]],
        source_ids=[second_source["id"]],
    )
    assert second.status_code == 201, second.text
    assert second.json()["parent_manifest_id"] == first["manifest_id"]
    assert second.json()["story_entries"][0]["rules"] == ["第二轮规则"]
    assert trace(client, run_id, through=1) == historic
    assert trace(client, run_id, through=0)["evidence"] == []
    assert evidence(client, run_id, **body).status_code == 200
    assert evidence(client, run_id, **{**body, "source_ids": []}).status_code == 409
    assert (
        evidence(client, run_id, story_entry_ids=[world["id"]]).json()["error"]["code"]
        == "CONTEXT_ROUND_CONFLICT"
    )
    assert client.get(path(run_id) + "/trace?through_round=3").status_code == 409
    assert client.get(path(run_id, "missing") + "/trace").status_code == 409
    with client.app.state.session_factory() as db:
        root = db.get(DependencyManifest, UUID(first["startup_manifest_id"]))
        assert "第二轮规则" not in json.dumps(root.snapshot_json, ensure_ascii=False)


def test_actual_provider_and_checkpoint_pin_each_stage_and_retry(
    client, episode, settings, monkeypatch, caplog
):
    item = entry(client, episode, rules=["取证"])
    run_id = run(client, episode)
    first = evidence(client, run_id, story_entry_ids=[item["id"]]).json()
    calls = []
    original = ContextTextProvider.check

    def check(self, stage, value):
        calls.append((stage, copy.deepcopy(self.contexts[stage])))
        return original(self, stage, value)

    monkeypatch.setattr(ContextTextProvider, "check", check)
    original_script = MockTextProvider.script

    def fail(*args, **kwargs):
        raise RuntimeError("Bearer private-provider-token data:image/png;base64,private-image")

    monkeypatch.setattr(MockTextProvider, "script", fail)
    assert advance(client, settings, run_id)["status"] == "FAILED"
    receipt = trace(client, run_id)["consumption"]
    assert receipt["evidence_manifest_ids"] == [first["manifest_id"]]
    second = evidence(client, run_id, expected_round=1, story_entry_ids=[item["id"]]).json()
    monkeypatch.setattr(MockTextProvider, "script", original_script)
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    assert calls[0] == calls[1]
    assert calls[1][1]["trace_ref"]["evidence_manifest_ids"] == [first["manifest_id"]]
    assert len(calls[1][1]["retrieval_evidence"]) == 1
    director = evidence(client, run_id, "storyboard", story_entry_ids=[item["id"]]).json()
    approved_script_id = state["pending_review"]["version_id"]
    assert review(client, state).status_code == 202
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    assert calls[-1][0] == "storyboard"
    assert calls[-1][1]["trace_ref"]["evidence_manifest_ids"] == [director["manifest_id"]]
    director_receipt = trace(client, run_id, "storyboard")["consumption"]
    resolved = director_receipt["resolved_input"]
    assert resolved["script_version_id"] == approved_script_id
    assert resolved["sources"][0]["asset_version_id"] == approved_script_id
    assert resolved["sha256"]
    with open_checkpointer(settings) as saver:
        records = list(saver.list({"configurable": {"thread_id": run_id}}))
    encoded = json.dumps(records, default=str)
    assert second["manifest_id"] not in encoded
    assert first["manifest_id"] in encoded and director["manifest_id"] in encoded
    for forbidden in (
        "private-provider-token",
        "private-image",
        "content_base64",
        "story-sources/",
    ):
        assert forbidden not in encoded + caplog.text
    assert trace(client, run_id)["consumption"] == receipt


@pytest.mark.parametrize("backend", ["local", "s3"])
def test_images_are_actual_authorized_objects_and_corruption_or_loss_is_refused(
    client, episode, settings, backend
):
    if backend == "s3":
        endpoint = os.getenv("ARCLIGHT_TEST_S3_ENDPOINT")
        if not endpoint:
            pytest.skip("Requires isolated S3 test service")
        settings.object_storage_backend = "s3"
        settings.s3_endpoint_url = endpoint
        settings.s3_access_key = type(settings.s3_access_key)("arclight-test")
        settings.s3_secret_key = type(settings.s3_secret_key)("arclight-test-secret")
        settings.s3_bucket = "arclight-test-ctx-" + uuid4().hex
        initialize(settings)
    try:
        asset = create_asset(client, episode["project_id"], "SCENE")
        with client.app.state.session_factory.begin() as db:
            original = make_asset(db, episode["project_id"], "CHARACTER")
        captured = capture(
            client,
            asset,
            [
                {
                    "source_asset_version_id": original["asset_version_id"],
                    "dependency_type": "REFERENCE_DEPENDENCY",
                }
            ],
        )
        assert captured.status_code == 201, captured.text
        manifest = captured.json()
        draft = save_image(client, asset, dependency_manifest_id=manifest["id"])
        ready = image_state(client, asset, draft, "READY_FOR_REVIEW").json()
        version = image_state(client, asset, ready, "APPROVED").json()
        run_id = run(client, episode)
        appended = evidence(client, run_id, asset_version_ids=[version["id"]])
        assert appended.status_code == 201, appended.text
        source = appended.json()["sources"][0]
        assert source["provenance"]["kind"] == "MANUAL_IMPORT"
        assert source["media_origin"]["rights_confirmed"] is True
        assert source["dependency_manifest_id"] == version["dependency_manifest_id"]
        assert source["dependency_manifest_sha256"]
        reference = source["object_ref"]
        assert reference["kind"] == "CONTROLLED_IMAGE"
        image_path = path(run_id) + "/images/" + reference["file_id"]
        image = client.get(image_path)
        assert image.status_code == 200 and image.content == image_bytes()
        assert image.headers["cache-control"] == "private, no-store"
        assert (
            client.get(path(run_id, "storyboard") + "/images/" + reference["file_id"]).status_code
            == 404
        )
        assert client.get(path(run_id) + "/images/" + str(uuid4())).status_code == 404
        serialized = json.dumps(trace(client, run_id))
        for forbidden in (
            "object_key",
            "http://",
            "X-Amz",
            "content_base64",
            base64.b64encode(image_bytes()).decode(),
        ):
            assert forbidden not in serialized
        if backend == "local":
            stored = next(settings.asset_storage_path.rglob("*.image"))
            stored.write_bytes(b"corrupt")
            assert client.get(image_path).status_code == 409
            assert (
                evidence(
                    client, run_id, expected_round=1, asset_version_ids=[version["id"]]
                ).status_code
                == 409
            )
            stored.unlink()
        else:
            with client.app.state.session_factory() as db:
                stored_file = db.get(StoredFile, UUID(reference["file_id"]))
                key = stored_file.object_key
            ObjectStorage(settings).delete(key)
        assert client.get(image_path).status_code == 409
        assert (
            evidence(
                client, run_id, expected_round=1, asset_version_ids=[version["id"]]
            ).status_code
            == 409
        )
        assert len(trace(client, run_id)["evidence"]) == 1
        assert advance(client, settings, run_id)["status"] == "FAILED"
        assert trace(client, run_id)["consumption"] is None
    finally:
        if backend == "s3":
            clear_test_bucket(ObjectStorage(settings))


def test_exact_text_assets_keep_their_original_dependency_manifest(client, episode):
    from test_text_contract_v2 import legacy_script

    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, episode["project_id"], "CHARACTER")
    content = legacy_script()
    content["character_assets"] = [{"character": "主角", "asset": reference}]
    script = save_script(client, episode, content)
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboard-runs",
        headers={"Idempotency-Key": "director"},
        json={"script_version_id": script["id"]},
    )
    assert response.status_code == 202, response.text
    sources = trace(client, response.json()["run_id"], "storyboard")["startup"]["sources"]
    source = next(item for item in sources if item["asset_version_id"] == script["id"])
    assert source["dependency_manifest_id"] and source["dependency_manifest_sha256"]
    assert source["object_ref"] is None


def test_foreign_missing_and_forbidden_evidence_is_atomically_rejected(client, episode):
    other = create_episode(client, create_project(client))
    foreign_entry = entry(client, other)
    foreign_source = save_text(client, other)
    with client.app.state.session_factory.begin() as db:
        foreign_asset = make_asset(db, other["project_id"], "SCENE")
    run_id = run(client, episode)
    for field, identity in (
        ("asset_version_ids", foreign_asset["asset_version_id"]),
        ("story_entry_ids", foreign_entry["id"]),
        ("source_ids", foreign_source["id"]),
    ):
        assert evidence(client, run_id, **{field: [identity]}).status_code == 404
        assert evidence(client, run_id, **{field: [str(uuid4())]}).status_code == 404
    assert trace(client, run_id)["evidence"] == []
    analysis = client.post(
        f"/api/v1/episodes/{other['id']}/story-analysis-runs",
        headers={"Idempotency-Key": "analyze"},
        json={"story": "故事"},
    ).json()["run_id"]
    assert (
        evidence(client, analysis, "analyze", story_entry_ids=[foreign_entry["id"]]).status_code
        == 422
    )
    assert (
        evidence(
            client, analysis, "analyze", asset_version_ids=[foreign_asset["asset_version_id"]]
        ).status_code
        == 422
    )


def test_viewer_can_trace_but_cannot_append_and_outsiders_cannot_read(client):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    item = entry(client, episode)
    asset = create_asset(client, episode["project_id"], "SCENE")
    draft = save_image(client, asset)
    ready = image_state(client, asset, draft, "READY_FOR_REVIEW").json()
    image = image_state(client, asset, ready, "APPROVED").json()
    run_id = run(client, episode)
    receipt = evidence(client, run_id, asset_version_ids=[image["id"]]).json()
    file_id = receipt["sources"][0]["object_ref"]["file_id"]
    image_path = path(run_id) + "/images/" + file_id
    viewer = register(client, "trace-viewer@example.com")
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
    assert trace(client, run_id)["trace_available"]
    assert client.get(image_path).content == image_bytes()
    assert evidence(client, run_id, story_entry_ids=[item["id"]]).status_code == 403
    register(client, "trace-outsider@example.com")
    assert client.get(path(run_id) + "/trace").status_code == 404
    assert client.get(image_path).status_code == 404
    assert evidence(client, run_id, story_entry_ids=[item["id"]]).status_code == 404


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"expected_round": True},
        {"expected_round": "0"},
        {"expected_round": 32},
        {"expected_round": -1},
        {"sources": [{"text": "untrusted claim"}]},
        {"story_entry_ids": [str(UUID(int=1))] * 2},
        {"content_base64": "YQ=="},
    ],
)
def test_evidence_request_contract_rejects_fabricated_or_invalid_inputs(client, episode, values):
    run_id = run(client, episode)
    response = evidence(client, run_id, **values)
    assert response.status_code == 422, response.text
    assert trace(client, run_id)["evidence"] == []


def test_cumulative_entry_asset_byte_and_round_limits_are_frozen(client, episode, settings):
    settings.context_limits = ContextLimits(max_bytes=16000, max_assets=1, max_story_entries=1)
    first_entry = entry(client, episode)
    second_entry = entry(client, episode, "CHARACTER")
    assert (
        client.patch(
            f"/api/v1/projects/{episode['project_id']}/story-entries/{second_entry['id']}",
            json={"expected_revision": 0, "archived": True},
        ).status_code
        == 200
    )
    run_id = run(client, episode)
    with client.app.state.session_factory.begin() as db:
        db.get(StoryEntry, UUID(second_entry["id"])).archived = False
        first_asset = make_asset(db, episode["project_id"], "SCENE")
        second_asset = make_asset(db, episode["project_id"], "PROP")
    assert (
        evidence(
            client,
            run_id,
            story_entry_ids=[first_entry["id"]],
            asset_version_ids=[first_asset["asset_version_id"]],
        ).status_code
        == 201
    )
    settings.context_limits = ContextLimits()
    assert (
        evidence(client, run_id, expected_round=1, story_entry_ids=[second_entry["id"]]).status_code
        == 422
    )
    assert (
        evidence(
            client, run_id, expected_round=1, asset_version_ids=[second_asset["asset_version_id"]]
        ).status_code
        == 422
    )
    source = save_text(
        client,
        episode,
        {**revise(client, episode, save_text(client, episode)), "text": "大" * 6000},
    )
    assert evidence(client, run_id, expected_round=1, source_ids=[source["id"]]).status_code == 422
    assert len(trace(client, run_id)["evidence"]) == 1
    # A separate stage can reuse one identity but can never accumulate round 33.
    for number in range(32):
        response = evidence(
            client, run_id, "storyboard", expected_round=number, story_entry_ids=[first_entry["id"]]
        )
        if response.status_code == 422:
            assert response.json()["error"]["code"] == "CONTEXT_TOO_LARGE"
            break  # This stage's frozen byte limit can reject before 32 rounds.
        assert response.status_code == 201, response.text
    assert len(trace(client, run_id, "storyboard")["evidence"]) <= 32


def test_missing_or_mismatched_origin_is_refused_before_execution(
    client, episode, settings, monkeypatch
):
    run_id = run(client, episode)
    original = context_sources.source_snapshot

    def wrong(*args):
        value = original(*args)
        return {**value, "sha256": "0" * 64}

    monkeypatch.setattr(context_sources, "source_snapshot", wrong)
    assert client.get(path(run_id) + "/trace").status_code == 409
    assert advance(client, settings, run_id)["error_code"] == "CONTEXT_REFERENCE_INVALID"


def test_legacy_trace_is_explicit_and_cannot_be_backfilled(client, episode, settings):
    item = entry(client, episode)

    def legacy(_mapper, _connection, row):
        if row.snapshot_json["context"].get("origin") == "context_builder":
            value = copy.deepcopy(row.snapshot_json)
            value["context"].pop("trace")
            row.snapshot_json = value
            row.request_fingerprint = digest(value)

    # Seed the real CTX-01 shape at INSERT, before database immutability applies.
    event.listen(DependencyManifest, "before_insert", legacy)
    try:
        run_id = run(client, episode)
    finally:
        event.remove(DependencyManifest, "before_insert", legacy)
    assert not trace(client, run_id)["trace_available"]
    assert trace(client, run_id)["startup"] is None
    assert "trace_ref" not in contexts(client, run_id)["contexts"]["script"]
    assert evidence(client, run_id, story_entry_ids=[item["id"]]).status_code == 409
    assert trace(client, run_id)["evidence"] == []
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"


def test_round_32_is_the_final_round_even_when_identity_is_reused(client, episode):
    item = entry(client, episode)
    run_id = run(client, episode)
    for number in range(32):
        result = evidence(client, run_id, expected_round=number, story_entry_ids=[item["id"]])
        assert result.status_code == 201, result.text
    assert len(trace(client, run_id)["evidence"]) == 32
    assert (
        evidence(client, run_id, expected_round=32, story_entry_ids=[item["id"]]).status_code == 422
    )


@pytest.mark.parametrize("unsafe", ["Bearer test-private-credential", "data:image/png;base64,YQ=="])
def test_untrusted_sources_cannot_persist_credentials_or_inline_images_in_evidence(
    client, episode, unsafe
):
    from test_story_source_revisions import text_payload

    source = save_text(client, episode, text_payload(unsafe))
    run_id = run(client, episode)
    response = evidence(client, run_id, source_ids=[source["id"]])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTEXT_INPUT_UNSAFE"
    assert trace(client, run_id)["evidence"] == []


def test_model_inline_image_output_never_reaches_artifacts_or_checkpoint(
    client, episode, settings, monkeypatch
):
    from arclight.persistence.models import Script

    original = MockTextProvider.script

    def unsafe(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        result.summary = "data:image/png;base64,private-model-image"
        return result

    monkeypatch.setattr(MockTextProvider, "script", unsafe)
    run_id = run(client, episode)
    assert advance(client, settings, run_id)["status"] == "FAILED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(Script).where(Script.run_id == UUID(run_id))) is None
    with open_checkpointer(settings) as saver:
        records = list(saver.list({"configurable": {"thread_id": run_id}}))
    assert "private-model-image" not in json.dumps(records, default=str)


def test_text_adapter_sends_exact_evidence_as_data_without_storage_locators(
    client, episode, settings, monkeypatch
):
    source = save_text(client, episode)
    run_id = run(client, episode)
    appended = evidence(client, run_id, source_ids=[source["id"]]).json()
    value = contexts(client, run_id)["contexts"]["script"]
    result = MockTextProvider().script("story", {}, 30000)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": result.model_dump_json()}}]}
        )

    monkeypatch.setattr(
        "arclight.providers.text.PublicHTTPTransport",
        lambda *args, **kwargs: httpx.MockTransport(handler),
    )
    OpenAICompatibleTextProvider(settings, {"script": value}).script("story", {}, 30000)
    sent = json.loads(calls[0]["messages"][1]["content"])
    actual = sent["task_context"]["retrieval_evidence"][0]
    assert actual["manifest_id"] == appended["manifest_id"]
    assert actual["stories"] == [{"source_id": source["id"], "text": source["text"]}]
    assert calls[0]["messages"][1]["role"] == "user"
    assert "story-sources/" not in json.dumps(calls)


def test_sql_guards_protect_startup_evidence_and_consumption(client, episode, settings):
    item = entry(client, episode)
    run_id = run(client, episode)
    evidence(client, run_id, story_entry_ids=[item["id"]])
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
    factory = client.app.state.session_factory
    with factory() as db:
        rows = list(db.scalars(select(DependencyManifest)).all())
    assert {row.snapshot_json["context"]["origin"] for row in rows} == {
        "context_builder",
        "context_evidence",
        "context_consumption",
    }
    for row in rows:
        for statement in (
            update(DependencyManifest)
            .where(DependencyManifest.id == row.id)
            .values(created_by="overwrite"),
            delete(DependencyManifest).where(DependencyManifest.id == row.id),
        ):
            with factory.begin() as db, pytest.raises(DBAPIError):
                db.execute(statement)
        if settings.database_url.startswith("sqlite"):
            table = DependencyManifest.__table__
            values = {column.name: getattr(row, column.name) for column in table.columns}
            with factory.begin() as db, pytest.raises(DBAPIError):
                db.execute(table.insert().prefix_with("OR REPLACE").values(**values))
    if settings.database_url.startswith("postgresql"):
        with factory.begin() as db, pytest.raises(DBAPIError):
            db.execute(text("TRUNCATE dependency_manifests CASCADE"))
    assert len(trace(client, run_id)["evidence"]) == 1


@pytest.mark.parametrize("replay", [False, True])
def test_postgres_concurrent_appends_have_one_round_and_one_receipt(
    client, episode, settings, replay
):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("Requires real PostgreSQL project locking")
    item = entry(client, episode)
    run_id = run(client, episode)
    keys = [str(uuid4()), str(uuid4())]
    if replay:
        keys[1] = keys[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(evidence, client, run_id, creation_key=key, story_entry_ids=[item["id"]])
            for key in keys
        ]
        results = [call.result() for call in calls]
    assert sorted(response.status_code for response in results) == (
        [200, 201] if replay else [201, 409]
    )
    assert len(trace(client, run_id)["evidence"]) == 1
