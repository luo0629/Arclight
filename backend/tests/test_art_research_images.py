"""Actual private bytes reach the original model transport; manifests retain only references."""

import base64
import json
import os
from uuid import UUID, uuid4

import httpx
import pytest
from storage_helpers import clear_test_bucket
from test_agent_model import FinalResult, call_response, model_request
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_art_research import add_image, contents, prepare_art, query, trace
from test_asset_versions import image_bytes
from test_assets import create as create_asset
from test_assets import path as asset_path
from test_generation_candidates import candidates, generate, ingest
from test_text_contract_v2 import make_asset

from arclight.application.agent_runtime import run_agent_loop
from arclight.application.agent_tools import build_agent_tool_registry
from arclight.application.asset_objects import object_path
from arclight.application.context_images import load_context_images
from arclight.core.errors import DomainError
from arclight.persistence.models import AssetVersion, WorkflowRun
from arclight.storage.objects import ObjectStorage, initialize


def test_eight_explicit_references_are_kept_without_silent_batch_trimming(
    client, episode, settings
):
    asset = create_asset(client, episode["project_id"])
    versions = [add_image(client, asset) for _ in range(8)]
    run_id, actor, _ = prepare_art(client, episode, settings)
    feedback = query(
        client, settings, run_id, {"path": "references", "version_ids": [v["id"] for v in versions]}
    )
    assert len(contents(feedback)) == len(feedback.result["image_file_ids"]) == 8
    assert not feedback.result["missing"]
    with client.app.state.session_factory() as db:
        images = load_context_images(
            db,
            settings,
            db.get(WorkflowRun, run_id),
            "storyboard",
            actor,
            list(map(UUID, feedback.result["image_file_ids"])),
        )
        assert len(images) == 8 and all(item.data == image_bytes() for item in images.values())


@pytest.mark.parametrize("storage", ["local", "s3"])
@pytest.mark.parametrize("include_images", [False, True])
def test_multiple_exact_images_have_real_bytes_and_safe_frozen_sources(
    client, episode, settings, storage, include_images, caplog
):
    if storage == "s3":
        endpoint = os.getenv("ARCLIGHT_TEST_S3_ENDPOINT")
        if not endpoint:
            pytest.skip("Requires isolated S3 test service")
        settings.object_storage_backend = "s3"
        settings.s3_endpoint_url = endpoint
        settings.s3_access_key = type(settings.s3_access_key)("arclight-test")
        settings.s3_secret_key = type(settings.s3_secret_key)("arclight-test-secret")
        settings.s3_bucket = "arclight-test-art-" + uuid4().hex
        initialize(settings)
    try:
        asset = create_asset(client, episode["project_id"])
        versions = [add_image(client, asset, color=color) for color in ("blue", "red")]
        run_id, actor, _ = prepare_art(client, episode, settings)
        selector = {
            "path": "references",
            "version_ids": [v["id"] for v in versions],
            "include_images": include_images,
        }
        feedback = query(client, settings, run_id, selector)
        materials = contents(feedback)
        assert len(materials) == 2 and not feedback.result["missing"]
        assert len(feedback.result["image_file_ids"]) == (2 if include_images else 0)
        file_ids = [UUID(item["image"]["file_id"]) for item in materials]
        assert all(item["rights_status"] == "CONFIRMED" for item in materials)
        with client.app.state.session_factory() as db:
            run = db.get(WorkflowRun, run_id)
            images = load_context_images(db, settings, run, "storyboard", actor, file_ids)
            assert [images[identity].data for identity in file_ids] == [
                image_bytes(color=color) for color in ("blue", "red")
            ]
            assert all(image.media_type == "image/png" for image in images.values())
        frozen = trace(client, run_id, actor)
        sources = frozen["evidence"][-1]["sources"]
        assert {s["asset_version_id"] for s in sources} == {v["id"] for v in versions}
        assert all(s["media_origin"]["rights_confirmed"] is True for s in sources)
        assert all(s["metadata_sha256"] and s["checksum"] for s in sources)
        encoded = json.dumps(frozen) + caplog.text
        for forbidden in (
            "object_key",
            "content_base64",
            "X-Amz",
            "s3/",
            base64.b64encode(image_bytes()).decode(),
        ):
            assert forbidden not in encoded
        before_materials = contents(feedback)
        fresh_asset = client.get(asset_path(asset["project_id"]) + "/" + asset["id"]).json()
        changed = client.put(
            asset_path(asset["project_id"]) + "/" + asset["id"],
            json={"name": "更新目录", "expected_revision": fresh_asset["revision"]},
        )
        assert changed.status_code == 200
        assert contents(query(client, settings, run_id, selector)) == before_materials
        assert (
            trace(client, run_id, actor)["evidence"][0]["art_investigation"]["result"]
            == feedback.result
        )
    finally:
        if storage == "s3":
            clear_test_bucket(ObjectStorage(settings))


@pytest.mark.parametrize("kind", ["absent", "no_image", "draft", "stale"])
def test_missing_or_unapproved_images_are_explicit_and_never_forwarded(
    client, episode, settings, kind
):
    if kind == "absent":
        version_id = str(uuid4())
        reason = "NOT_FOUND"
    elif kind == "no_image":
        with client.app.state.session_factory.begin() as db:
            version_id = make_asset(db, episode["project_id"], "PROP")["asset_version_id"]
        reason = "NO_IMAGE"
    else:
        asset = create_asset(client, episode["project_id"])
        version = add_image(client, asset, approve=kind != "draft")
        version_id = version["id"]
        if kind == "stale":
            with client.app.state.session_factory.begin() as db:
                db.get(AssetVersion, UUID(version_id)).stale = True
        reason = "INPUT_NOT_USABLE"
    run_id, _, _ = prepare_art(client, episode, settings)
    feedback = query(client, settings, run_id, {"path": "references", "version_ids": [version_id]})
    assert feedback.status == "SUCCEEDED", feedback.model_dump_json()
    assert feedback.result["missing"] == [
        {"selector": {"asset_version_id": version_id}, "reason": reason}
    ]
    assert feedback.result["image_file_ids"] == []


def test_generation_origin_does_not_fabricate_rights_even_for_approved_status(
    client, episode, settings
):
    asset = create_asset(client, episode["project_id"])
    job, attempt = generate(client, episode["project_id"], asset)
    assert ingest(client, attempt) == 1
    version_id = candidates(client, episode["project_id"], job)[0]["asset_version_id"]
    # Isolate the rights gate from the separate continuity/review gate. The
    # genuine importer produced private bytes with an explicit unconfirmed grant.
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(version_id)).status = "APPROVED"
    run_id, actor, _ = prepare_art(client, episode, settings)
    for path in ("authorization", "references"):
        feedback = query(client, settings, run_id, {"path": path, "version_ids": [version_id]})
        assert contents(feedback)[0]["rights_status"] == "UNDECLARED"
        assert feedback.result["missing"][0]["reason"] == "RIGHTS_UNDECLARED"
        assert feedback.result["image_file_ids"] == []
    assert all(not round_["sources"] for round_ in trace(client, run_id, actor)["evidence"])


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_object_loss_is_frozen_as_missing_and_old_evidence_is_preserved(
    client, episode, settings, damage
):
    asset = create_asset(client, episode["project_id"])
    version = add_image(client, asset)
    run_id, actor, _ = prepare_art(client, episode, settings)
    selector = {"path": "references", "version_ids": [version["id"]]}
    original = query(client, settings, run_id, selector)
    assert len(contents(original)) == 1
    stored = object_path(
        settings.asset_storage_path, UUID(episode["project_id"]), UUID(version["id"])
    )
    if damage == "missing":
        stored.unlink()
    else:
        stored.write_bytes(b"corrupt")
    cached = query(client, settings, run_id, selector)
    assert cached.status == "FAILED" and cached.error.code == "CONTEXT_INPUT_NOT_USABLE"
    missing = query(client, settings, run_id, selector, refresh=True)
    assert contents(missing)[0]["asset_version_id"] == version["id"]
    assert missing.result["missing"][0]["reason"] == "OBJECT_UNAVAILABLE"
    assert missing.result["image_file_ids"] == []
    assert (
        trace(client, run_id, actor)["evidence"][0]["art_investigation"]["result"]
        == original.result
    )


@pytest.mark.parametrize(
    "change,code",
    [
        ("stale", "CONTEXT_INPUT_NOT_USABLE"),
        ("archive", "CONTEXT_INPUT_NOT_USABLE"),
        ("scope", "CONTEXT_SCOPE_NOT_ALLOWED"),
    ],
)
def test_model_object_loading_rechecks_lifecycle_and_episode_scope(
    client, episode, settings, change, code
):
    from arclight.persistence.models import VisualAsset

    asset = create_asset(client, episode["project_id"])
    version = add_image(client, asset)
    run_id, actor, _ = prepare_art(client, episode, settings)
    first = query(client, settings, run_id, {"path": "references", "version_ids": [version["id"]]})
    assert len(contents(first)) == 1
    with client.app.state.session_factory.begin() as db:
        if change == "stale":
            db.get(AssetVersion, UUID(version["id"])).stale = True
        elif change == "archive":
            db.get(VisualAsset, UUID(asset["id"])).status = "ARCHIVED"
        else:
            row = db.get(VisualAsset, UUID(asset["id"]))
            row.metadata_json = {
                **row.metadata_json,
                "usage_scope": {"kind": "EPISODES", "episode_ids": [str(uuid4())]},
            }
    with client.app.state.session_factory() as db:
        with pytest.raises(DomainError) as error:
            load_context_images(
                db,
                settings,
                db.get(WorkflowRun, run_id),
                "storyboard",
                actor,
                list(map(UUID, first.result["image_file_ids"])),
            )
        assert error.value.code == code


def test_original_runtime_delivers_two_actual_images_once_and_persists_no_binary(
    client, episode, settings, model_service
):
    asset = create_asset(client, episode["project_id"])
    versions = [add_image(client, asset, color=color) for color in ("blue", "red")]
    run_id, actor, _ = prepare_art(client, episode, settings)
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if not tool_feedback(payload):
            arguments = {
                "query": {"path": "references", "version_ids": [v["id"] for v in versions]}
            }
            value = call_response(
                ("art-images-1", "investigate_art", arguments),
                ("art-images-2", "investigate_art", arguments),
            )
        else:
            image_urls = [
                part["image_url"]["url"]
                for message in payload["messages"]
                if isinstance(message.get("content"), list)
                for part in message["content"]
                if part["type"] == "image_url"
            ]
            assert image_urls == [
                "data:image/png;base64," + base64.b64encode(image_bytes(color=color)).decode()
                for color in ("blue", "red")
            ]
            value = final()
        return httpx.Response(200, json=value)

    model_service(respond)
    result = run_agent_loop(
        client.app.state.session_factory,
        settings,
        model_request(run_id, agent_name="art_director", stage="storyboard"),
        build_agent_tool_registry(settings=settings),
        FinalResult,
    )
    assert result.status == "COMPLETED" and len(sent) == 2
    frozen = trace(client, run_id, actor)
    assert len(frozen["evidence"]) == 2
    encoded = json.dumps(frozen)
    assert base64.b64encode(image_bytes()).decode() not in encoded
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.pending_review is None and run.status == "RUNNING"
        assert "content_base64" not in json.dumps(run.agent_state_json)


@pytest.mark.parametrize("limit", ["count", "single", "total"])
def test_model_capacity_is_enforced_before_second_transport_call(
    client, episode, settings, model_service, limit
):
    patch = {
        "count": {"max_input_images": 1},
        "single": {"max_image_bytes": 1},
        "total": {"max_total_image_bytes": 1},
    }[limit]
    settings.llm_agent_capabilities = settings.llm_agent_capabilities.model_copy(update=patch)
    asset = create_asset(client, episode["project_id"])
    versions = [add_image(client, asset, color=color) for color in ("blue", "red")]
    run_id, _, _ = prepare_art(client, episode, settings)
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(
            200,
            json=call_response(
                (
                    "art-images",
                    "investigate_art",
                    {"query": {"path": "references", "version_ids": [v["id"] for v in versions]}},
                )
            ),
        )

    model_service(respond)
    result = run_agent_loop(
        client.app.state.session_factory,
        settings,
        model_request(run_id, agent_name="art_director", stage="storyboard"),
        build_agent_tool_registry(settings=settings),
        FinalResult,
    )
    assert result.status == "STOPPED" and result.stop_code == "GATEWAY_CAPABILITY_REJECTED"
    assert len(sent) == 1
