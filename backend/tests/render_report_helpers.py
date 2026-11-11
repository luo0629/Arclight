"""Actual formal renders and durable Owner decisions for delivery-report tests."""

from copy import deepcopy
from uuid import UUID

import pytest
import test_continuity_reviews
from render_admission_helpers import ready_manifest, request_export
from render_job_helpers import read, run, submit
from render_manifest_helpers import render_case, review_case, timeline_case
from test_human_reviews import decide, resume
from test_text_contract_v2 import legacy_script

from arclight.application.render_inputs import file_storage
from arclight.persistence.models import StoredFile

__all__ = [
    "formal_report_case",
    "no_dialogue_render_case",
    "render_case",
    "review_case",
    "timeline_case",
]


def delivery_url(episode, artifact_id):
    return f"/api/v1/episodes/{episode['id']}/deliveries/{artifact_id}"


def report_url(episode, artifact_id):
    return delivery_url(episode, artifact_id) + "/report"


def report_read(client, episode, artifact_id):
    response = client.get(report_url(episode, artifact_id))
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    return response.json()


def production_state(client, episode):
    response = client.get(f"/api/v1/episodes/{episode['id']}/production-state")
    assert response.status_code == 200, response.text
    return response.json()


def formal_job(client, timeline):
    manifest, _ = ready_manifest(client, timeline, profile="FORMAL")
    requested = request_export(client, manifest)
    saved = decide(client, requested)
    assert saved.status_code == 200, saved.text
    resumed = resume(client, requested, saved.json())
    assert resumed.status_code == 200, resumed.text
    value, _ = submit(client, manifest)
    return manifest, value


def completed_report(client, episode, timeline):
    manifest, value = formal_job(client, timeline)
    assert run(client, value) == "SUCCEEDED", read(client, value)
    final = read(client, value)
    return manifest, final, report_read(client, episode, final["artifact_id"])


def report_bytes(client, report):
    with client.app.state.session_factory() as db:
        row = db.get(StoredFile, UUID(report["report_file_id"]))
        return file_storage(client.app.state.settings, row).read(
            row.object_key, row.byte_size, row.checksum
        )


def request_warnings(client, episode, artifact_id):
    response = client.post(delivery_url(episode, artifact_id) + "/warning-review")
    assert response.status_code in {200, 201}, response.text
    return response.json()


def accept_warnings(client, episode, artifact_id):
    requested = request_warnings(client, episode, artifact_id)
    saved = decide(client, requested)
    assert saved.status_code == 200, saved.text
    resumed = resume(client, requested, saved.json())
    assert resumed.status_code == 200, resumed.text
    return requested, saved.json(), resumed.json()


@pytest.fixture
def formal_report_case(client, episode, render_case):
    return completed_report(client, episode, render_case)


@pytest.fixture
def no_dialogue_render_case(client, episode, monkeypatch, request):
    """Create a legitimate silent scene through the normal text and media APIs."""
    save_script = test_continuity_reviews.save_script
    content = deepcopy(legacy_script())
    for scene in content["scenes"]:
        scene["dialogue"] = []

    def without_dialogue(api, target):
        return save_script(api, target, content=content)

    monkeypatch.setattr(test_continuity_reviews, "save_script", without_dialogue)
    return request.getfixturevalue("render_case")
