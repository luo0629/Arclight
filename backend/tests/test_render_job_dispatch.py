"""The installed Celery entry points use durable render rows and isolate preview traffic."""

from uuid import UUID

import pytest
from render_job_helpers import job_case, read, render_case, review_case, timeline_case

from arclight.application.render_dispatch import candidates
from arclight.application.rendering import execute_preview
from arclight.core.errors import DomainError
from arclight.workers import tasks

__all__ = ["job_case", "render_case", "review_case", "timeline_case"]


@pytest.fixture
def publishing(client, monkeypatch):
    monkeypatch.setattr(tasks, "get_settings", lambda: client.app.state.settings)
    published, previews = [], []
    monkeypatch.setattr(
        tasks.execute_manifest_render, "apply_async", lambda args: published.append(args)
    )
    monkeypatch.setattr(
        tasks.execute_render_preview, "apply_async", lambda args: previews.append(args)
    )
    return published, previews


def test_actual_dispatcher_redelivers_until_claim_and_worker_entry_is_idempotent(
    client, job_case, publishing
):
    manifest, value, _ = job_case
    published, previews = publishing
    tasks.dispatch_outbox.run()
    tasks.dispatch_outbox.run()
    assert published == [[value["id"]], [value["id"]]] and previews == []
    assert tasks.execute_manifest_render.run(value["id"]) == "SUCCEEDED"
    assert tasks.execute_manifest_render.run(value["id"]) == "SUCCEEDED"
    final = read(client, value)
    assert final["attempt_count"] == 1 and final["manifest_id"] == manifest["id"]
    tasks.dispatch_outbox.run()
    assert len(published) == 2 and previews == []
    # A manifest ID cannot accidentally reach the old single-image producer.
    with pytest.raises(DomainError) as error:
        execute_preview(
            client.app.state.session_factory, client.app.state.settings, UUID(value["id"])
        )
    assert error.value.code == "NOT_FOUND" and read(client, value) == final
    root = f"/api/v1/projects/{manifest['project_id']}/render-previews/{value['id']}"
    assert client.get(root).status_code == 404


def test_broker_failure_keeps_queued_intent_and_next_scan_publishes(
    client, job_case, publishing, monkeypatch
):
    _, value, _ = job_case

    def unavailable(**kwargs):
        raise OSError("broker unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(tasks.execute_manifest_render, "apply_async", unavailable)
        with pytest.raises(OSError, match="broker unavailable"):
            tasks.dispatch_outbox.run()
    assert read(client, value)["status"] == "QUEUED"
    assert UUID(value["id"]) in candidates(client.app.state.session_factory)
    tasks.dispatch_outbox.run()
    assert publishing == ([[value["id"]]], [])
