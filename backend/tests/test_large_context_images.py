"""Large approved images keep exact context authority through the real model request."""

import base64
import hashlib
import json
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from test_agent_model import (
    FinalResult,
    active_run,
    final_response,
    model_request,
    model_service,
    receipts,
)
from test_asset_versions import save, state
from test_assets import create as create_asset
from test_context_trace import evidence
from test_media_file_sizes import large_png

from arclight.application.agent_tools import build_agent_tool_registry
from arclight.application.context_images import load_context_images
from arclight.application.gateway_agent import invoke_agent
from arclight.core.errors import DomainError
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import StoredFile, WorkflowRun

__all__ = ["large_png", "model_service"]


def approve_image(client, asset, version):
    for status in ("READY_FOR_REVIEW", "APPROVED"):
        response = state(client, asset, version, status)
        assert response.status_code == 200, response.text
        version = response.json()
    return version


def test_large_exact_approved_context_image_reaches_model_without_a_product_byte_ceiling(
    client, episode, settings, tmp_path, large_png, model_service, caplog
):
    settings.asset_storage_path = tmp_path / "objects"
    encoded = base64.b64encode(large_png).decode("ascii")
    checksum = hashlib.sha256(large_png).hexdigest()
    assert len(large_png) > 1024**2
    assert len(encoded) > settings.max_request_bytes
    asset = create_asset(client, episode["project_id"], "SCENE")
    version = approve_image(client, asset, save(client, asset, content_base64=encoded))
    assert version["checksum"] == checksum

    # A newer approved image of the same asset cannot replace the pinned historical one.
    current = client.get(f"/api/v1/projects/{episode['project_id']}/assets/{asset['id']}")
    assert current.status_code == 200, current.text
    newer = approve_image(client, current.json(), save(client, current.json()))
    assert newer["id"] != version["id"]
    run_id, actor = active_run(client, episode)
    added = evidence(client, str(run_id), asset_version_ids=[version["id"]])
    assert added.status_code == 201, added.text
    references = [source["object_ref"] for source in added.json()["sources"]]
    assert len(references) == 1
    reference = references[0]
    assert reference["asset_version_id"] == version["id"]
    assert reference["checksum"] == checksum and reference["byte_size"] == len(large_png)
    identity = UUID(reference["file_id"])
    capabilities = settings.llm_agent_capabilities
    assert capabilities.max_image_bytes is None
    assert capabilities.max_total_image_bytes is None
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        images = load_context_images(
            db,
            settings,
            run,
            "script",
            actor,
            [identity],
            through_round=1,
            capabilities=capabilities,
        )
        assert list(images) == [identity]
        assert images[identity].data == large_png
        unpinned = db.scalar(
            select(StoredFile).where(StoredFile.asset_version_id == UUID(newer["id"]))
        )
        unpinned_id = unpinned.id
        with pytest.raises(DomainError) as denied:
            load_context_images(
                db,
                settings,
                run,
                "script",
                actor,
                [unpinned_id],
                through_round=1,
                capabilities=capabilities,
            )
        assert denied.value.code == "NOT_FOUND" and denied.value.status_code == 404

    received = []

    def handler(request):
        payload = json.loads(request.content)
        blocks = payload["messages"][1]["content"]
        urls = [block["image_url"]["url"] for block in blocks if block["type"] == "image_url"]
        assert urls == ["data:image/png;base64," + encoded]
        decoded = base64.b64decode(urls[0].split(",", 1)[1], validate=True)
        assert decoded == large_png
        received.append((len(decoded), hashlib.sha256(decoded).hexdigest()))
        return httpx.Response(200, json=final_response())

    model_service(handler)
    registry = build_agent_tool_registry()
    with pytest.raises(DomainError) as denied:
        invoke_agent(
            client.app.state.session_factory,
            settings,
            model_request(
                run_id,
                evidence_round=1,
                messages=[{"role": "user", "data": {}, "image_file_ids": [str(unpinned_id)]}],
            ),
            registry,
            FinalResult,
        )
    assert denied.value.code == "NOT_FOUND" and denied.value.status_code == 404
    assert received == [] and receipts(client, run_id) == []
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(GatewayCall)
                .where(GatewayCall.workflow_id == run_id)
            )
            == 0
        )

    request = model_request(
        run_id,
        evidence_round=1,
        messages=[{"role": "user", "data": {}, "image_file_ids": [str(identity)]}],
    )
    result = invoke_agent(
        client.app.state.session_factory, settings, request, registry, FinalResult
    )
    assert result.status == "FINAL"
    assert received == [(len(large_png), checksum)]
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        call = db.scalar(select(GatewayCall).where(GatewayCall.workflow_id == run_id))
        assert call.parameters_json["input_images"] == 1
        persisted = json.dumps(
            [run.input_json, run.agent_state_json, call.route_json, call.parameters_json]
        )
    assert encoded not in persisted + caplog.text + repr(request)
    assert "data:image" not in persisted + caplog.text
