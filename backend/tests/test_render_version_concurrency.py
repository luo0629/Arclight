"""Independent PostgreSQL render jobs append versions of one exact manifest."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from threading import Barrier
from uuid import UUID

import pytest
from render_job_helpers import job_case, read, render_case, review_case, run, submit, timeline_case
from sqlalchemy import select
from test_concurrency import require_postgres

from arclight.application import render_execution
from arclight.application.render_inputs import file_storage
from arclight.persistence.models import AssetDependency, AssetVersion, RenderArtifact, StoredFile

__all__ = ["job_case", "render_case", "review_case", "timeline_case"]


@pytest.fixture
def postgres_render_jobs(settings, request):
    require_postgres(settings)
    return request.getfixturevalue("job_case")


def test_postgres_same_manifest_concurrent_jobs_append_independent_immutable_versions(
    client, postgres_render_jobs, monkeypatch
):
    manifest, first, _ = postgres_render_jobs
    second, _ = submit(client, manifest)
    factory = client.app.state.session_factory
    source_ids = {UUID(source["asset_version_id"]) for source in manifest["snapshot"]["sources"]}
    source_ids.add(UUID(manifest["timeline_id"]))
    with factory() as db:
        before = {
            row.id: (row.object_key, row.checksum, deepcopy(row.metadata_json))
            for row in db.scalars(select(AssetVersion).where(AssetVersion.id.in_(source_ids)))
        }
    completed_encoding = Barrier(2)
    real_encode = render_execution.execute_media

    def encode(*args, **kwargs):
        result = real_encode(*args, **kwargs)
        completed_encoding.wait(timeout=60)
        return result

    monkeypatch.setattr(render_execution, "execute_media", encode)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: run(client, value), (first, second)))
    assert results == ["SUCCEEDED", "SUCCEEDED"]
    final = [read(client, value) for value in (first, second)]
    assert all(value["output_asset_version_id"] and value["artifact_id"] for value in final)
    with factory() as db:
        receipts = list(
            db.scalars(
                select(RenderArtifact).order_by(RenderArtifact.created_at, RenderArtifact.id)
            )
        )
        assert len(receipts) == 2
        assert {row.job_id for row in receipts} == {UUID(first["id"]), UUID(second["id"])}
        assert {row.manifest_id for row in receipts} == {UUID(manifest["id"])}
        versions = [db.get(AssetVersion, row.asset_version_id) for row in receipts]
        assert sorted(row.version_no for row in versions) == [1, 2]
        assert len({row.asset_id for row in versions}) == 1
        assert len({row.id for row in versions}) == len({row.object_key for row in versions}) == 2
        for receipt, version in zip(receipts, versions, strict=True):
            file = db.get(StoredFile, receipt.file_id)
            data = file_storage(client.app.state.settings, file).read(
                file.object_key, 128 * 1024 * 1024, file.checksum
            )
            assert sha256(data).hexdigest() == receipt.checksum == version.checksum
            assert len(data) == file.byte_size == receipt.byte_size
            assert file.asset_version_id == version.id
            consumed = set(
                db.scalars(
                    select(AssetDependency.source_asset_version_id).where(
                        AssetDependency.target_asset_version_id == version.id
                    )
                )
            )
            assert consumed == source_ids
        after = {
            row.id: (row.object_key, row.checksum, row.metadata_json)
            for row in db.scalars(select(AssetVersion).where(AssetVersion.id.in_(source_ids)))
        }
        assert after == before
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest
    for value in final:
        assert run(client, value) == "SUCCEEDED"
        assert read(client, value) == value
    with factory() as db:
        assert len(list(db.scalars(select(RenderArtifact.id)))) == 2
