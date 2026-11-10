"""Independent real PostgreSQL transactions share the existing project write gate."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import (
    manifest_url,
    render_case,
    report_bytes,
    result_case,
    review_case,
    stored_file,
    timeline_case,
)
from sqlalchemy import func, select
from test_concurrency import race, require_postgres

from arclight.application.render_results import bind_render_artifact, bind_render_report
from arclight.core.errors import DomainError
from arclight.persistence.models import AuditEvent
from arclight.persistence.render_manifests import RenderArtifact, RenderManifest, RenderReportLink

__all__ = ["render_case", "result_case", "review_case", "timeline_case"]


@pytest.fixture
def postgres_render(settings, request):
    require_postgres(settings)
    return request.getfixturevalue("render_case")


def test_postgres_duplicate_capture_publishes_one_manifest_and_audit(client, postgres_render):
    responses = race(
        client.app,
        manifest_url(postgres_render),
        {"creation_key": str(uuid4())},
        dict(client.headers),
    )
    assert [row.status_code for row in responses] == [201, 201], [row.text for row in responses]
    assert responses[0].json() == responses[1].json()
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderManifest.id))) == 1
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == "render.manifest_created"
                )
            )
            == 1
        )


@pytest.mark.parametrize("target", ["artifact", "report"])
@pytest.mark.parametrize("duplicate", [True, False])
def test_postgres_result_races_keep_one_receipt_and_exact_replay(
    client, episode, postgres_render, request, target, duplicate
):
    # The existing receipt is used only to obtain exact fixture bytes. Each race
    # targets a fresh manifest/parent and unbound files, through trusted writers.
    from render_manifest_helpers import manifest

    value, previous, data = request.getfixturevalue("result_case")
    if target == "artifact":
        value, _ = manifest(client, postgres_render)
    else:
        data = report_bytes(value, previous)
    media_type = "video/mp4" if target == "artifact" else "application/json"
    first, _, _ = stored_file(client, episode["project_id"], data, media_type)
    second = first if duplicate else stored_file(client, episode["project_id"], data, media_type)[0]
    barrier = Barrier(2)

    def bind(file_id):
        barrier.wait(timeout=20)
        try:
            with client.app.state.session_factory.begin() as db:
                args = (file_id, 2500) if target == "artifact" else (file_id,)
                writer = bind_render_artifact if target == "artifact" else bind_render_report
                return writer(
                    db, client.app.state.settings, UUID(value["id"]), *args, "service-owner"
                )
        except DomainError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, (first, second)))
    successes = [result for result in results if not isinstance(result, DomainError)]
    if duplicate:
        assert len(successes) == 2 and successes[0] == successes[1]
    else:
        assert len(successes) == 1
        assert [result.code for result in results if isinstance(result, DomainError)] == [
            "RENDER_RESULT_CONFLICT"
        ]
    model = RenderArtifact if target == "artifact" else RenderReportLink
    parent = (
        model.manifest_id == UUID(value["id"])
        if target == "artifact"
        else (model.artifact_id == previous.id)
    )
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(model.id)).where(parent)) == 1
        row = db.scalar(select(model).where(parent))
        assert row.file_id in {first, second}
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == f"render.{target}_bound", AuditEvent.resource_id == row.id
                )
            )
            == 1
        )
