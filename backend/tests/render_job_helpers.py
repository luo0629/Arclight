"""Real manifests and durable execution controls for renderer acceptance tests."""

from uuid import UUID, uuid4

import pytest
from render_admission_helpers import ready_manifest, render_path
from render_manifest_helpers import render_case, review_case, timeline_case

from arclight.application.render_execution import execute_render
from arclight.persistence.models import RenderJob

__all__ = ["render_case", "review_case", "timeline_case", "job_case"]


def submit(client, manifest, **overrides):
    body = {"creation_key": str(uuid4()), **overrides}
    response = client.post(render_path(manifest, "jobs"), json=body)
    assert response.status_code == 202, response.text
    return response.json(), body


@pytest.fixture
def job_case(client, render_case):
    manifest, _ = ready_manifest(client, render_case)
    value, body = submit(client, manifest)
    return manifest, value, body


def run(client, value):
    return execute_render(
        client.app.state.session_factory, client.app.state.settings, UUID(value["id"])
    )


def read(client, value):
    response = client.get(f"/api/v1/render-jobs/{value['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def cancel(client, value):
    current = read(client, value)
    return client.post(
        f"/api/v1/render-jobs/{value['id']}/cancel",
        json={"expected_revision": current["revision"]},
    )


def expire(client, value):
    from datetime import timedelta

    from arclight.application.generation_leases import clock

    with client.app.state.session_factory.begin() as db:
        db.get(RenderJob, UUID(value["id"])).lease_expires_at = clock(db) - timedelta(seconds=1)
