"""One explicitly authorized synthetic image, isolated from all business databases."""

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from test_generation_budget import set_policy
from test_generation_jobs import capture_ok
from test_generation_queues import messages
from test_openai_images import attempt_for

from arclight.core.image_service import OPENAI_IMAGE_MODEL, OpenAIImageSettings
from arclight.workers import tasks

pytestmark = pytest.mark.skipif(
    os.getenv("ARCLIGHT_LIVE_IMAGE_APPROVAL") != "single-synthetic-image",
    reason="Real paid image acceptance requires explicit cost/data authorization",
)


def test_one_authorized_synthetic_image(settings, client, episode, monkeypatch, tmp_path):
    # Never use a production URL or pick up account/server keys from an .env file.
    assert settings.database_url.startswith("sqlite:///"), (
        "Live acceptance requires isolated SQLite"
    )
    key_file = Path(os.environ["ARCLIGHT_LIVE_IMAGE_KEY_FILE"])
    quote = Decimal(os.environ["ARCLIGHT_LIVE_IMAGE_MAX_USD"])
    assert quote.is_finite() and Decimal("0") < quote <= Decimal("10")
    settings.image_service = OpenAIImageSettings(
        enabled=True,
        base_url=os.getenv("ARCLIGHT_LIVE_IMAGE_BASE_URL", "https://api.openai.com/v1"),
        model=os.getenv("ARCLIGHT_LIVE_IMAGE_MODEL", OPENAI_IMAGE_MODEL),
        api_key_file=key_file,
        maximum_cost_per_image=quote,
        pricing_version=os.environ["ARCLIGHT_LIVE_IMAGE_PRICING_VERSION"],
        timeout_seconds=180,
    )
    settings.image_service.require_ready()
    settings.media_execution_policy = settings.media_execution_policy.model_copy(
        update={"automatic_retry_errors": frozenset(), "call_timeout_seconds": 180}
    )
    settings.model_gateway_policy = settings.model_gateway_policy.model_copy(
        update={"max_attempts": 1, "timeout_seconds": 180}
    )
    project_id = episode["project_id"]
    set_policy(client, project_id, str(quote), task_limit=str(quote))
    body = {
        "target_type": "SHOT_IMAGE",
        "task_type": "IMAGE",
        "spec": {
            "provider": "openai",
            "model": settings.image_service.model,
            "model_config_version": settings.image_service.public_configuration()[
                "configuration_version"
            ],
            "prompt": "A single blue circle centered on a plain white background. No text.",
            "prompt_version": "gen09-synthetic-acceptance:1",
            "parameters": {"quality": "low"},
            "output_spec": {"width": 1024, "height": 1024, "format": "jpeg"},
            "batch_size": 1,
        },
    }
    job = capture_ok(client, project_id, body)
    message = messages(client)[0]
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    args = (str(message[0]), str(message[1]), str(message[2]), message[3])
    status = tasks.execute_generation.run(*args)
    attempt = attempt_for(client, job)
    evidence = {
        "job_id": job["id"],
        "attempt_id": str(attempt.id),
        "status": status,
        "provider_request_id": attempt.provider_request_id,
        "usage": attempt.result_json["usage"] if attempt.result_json else None,
        "actual_cost": None,
        "reserved_usd": str(quote),
        "model": settings.image_service.model,
        "base_url": settings.image_service.base_url,
        "pricing_version": settings.image_service.pricing_version,
    }
    evidence_file = tmp_path / "live-image-evidence.json"
    evidence_file.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert status == "SUCCEEDED", f"Retained evidence: {evidence_file}; never retry automatically"
    evidence["duplicate_delivery_status"] = tasks.execute_generation.run(*args)
    assert evidence["duplicate_delivery_status"] == "SUCCEEDED"
    evidence["imported_candidates"] = tasks.import_generation_candidates.run(str(attempt.id))
    evidence["duplicate_imported_candidates"] = tasks.import_generation_candidates.run(
        str(attempt.id)
    )
    assert evidence["imported_candidates"] == 1
    assert evidence["duplicate_imported_candidates"] == 0
    rows = client.get(
        f"/api/v1/projects/{project_id}/generation-jobs/{job['id']}/candidates"
    ).json()
    evidence["candidates"] = [
        {key: row[key] for key in ("id", "asset_version_id", "error_code", "required_review")}
        for row in rows
    ]
    evidence["cost_summary"] = client.get(f"/api/v1/projects/{project_id}/costs").json()["summary"]
    evidence_file.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert len(rows) == 1 and rows[0]["error_code"] is None
    assert rows[0]["required_review"] == "CONTINUITY"
    image = client.get(f"/api/v1/projects/{project_id}/generation-candidates/{rows[0]['id']}/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    (tmp_path / "synthetic-image.jpeg").write_bytes(image.content)
    evidence["image_sha256"] = hashlib.sha256(image.content).hexdigest()
    evidence["candidate_id"] = rows[0]["id"]
    evidence_file.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
