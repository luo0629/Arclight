"""VIDEO-01 real PostgreSQL recovery locking and replacement CAS evidence."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_concurrency import require_postgres
from test_generation_video_inputs import video_case as video_case  # noqa: F401
from test_video_candidate_api import manual_file
from test_video_candidates import finish_video, video_bytes
from test_video_candidates import formal_video_case as formal_video_case  # noqa: F401

from arclight.application import generation_artifacts, video_candidates
from arclight.persistence.models import (
    AssetVersion,
    GenerationCandidate,
    GenerationMediaArtifact,
    VisualAsset,
)


@pytest.fixture
def postgres_video_case(settings, request):
    require_postgres(settings)
    return request.getfixturevalue("formal_video_case")


def test_parallel_existing_video_artifact_recovery_registers_one_candidate_and_version(
    client, settings, postgres_video_case, monkeypatch
):
    factory = client.app.state.session_factory
    _, attempt, provider = finish_video(client, settings, postgres_video_case, video_bytes())
    # Model a worker stopping after the real immutable artifact is committed.
    with monkeypatch.context() as patch:
        patch.setattr(video_candidates, "import_video_candidates", lambda *_: 0)
        assert generation_artifacts.import_attempt(factory, settings, attempt, provider) == 1
    with factory() as db:
        artifact = db.scalar(
            select(GenerationMediaArtifact).where(GenerationMediaArtifact.attempt_id == attempt)
        )
        assert artifact is not None and artifact.error_code is None
        artifact_id, file_id, original_media = artifact.id, artifact.file_id, artifact.media_json
        assert (
            db.scalar(
                select(func.count())
                .select_from(GenerationCandidate)
                .where(GenerationCandidate.attempt_id == attempt)
            )
            == 0
        )

    validated = video_candidates.validated_video
    ready_to_write = Barrier(2)

    def read_before_race(*args):
        result = validated(*args)
        # Both sessions saw the same missing candidate and read real stored bytes;
        # the production dependency-graph lock and second existence check arbitrate.
        ready_to_write.wait(timeout=30)
        return result

    monkeypatch.setattr(video_candidates, "validated_video", read_before_race)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(video_candidates.import_video_candidates, factory, settings, attempt)
            for _ in range(2)
        ]
        assert sorted(future.result(timeout=90) for future in futures) == [0, 1]

    with factory() as db:
        [candidate] = list(
            db.scalars(select(GenerationCandidate).where(GenerationCandidate.attempt_id == attempt))
        )
        assert candidate.ordinal == 0 and candidate.error_code is None
        assert candidate.source == "GENERATED" and candidate.asset_version_id is not None
        version = db.get(AssetVersion, candidate.asset_version_id)
        assert version is not None and version.status == "DRAFT"
        asset = db.get(VisualAsset, version.asset_id)
        assert asset is not None and asset.asset_type == "VIDEO"
        assert asset.current_version_id is None and asset.revision == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AssetVersion)
                .where(AssetVersion.asset_id == asset.id)
            )
            == 1
        )
        [artifact] = list(
            db.scalars(
                select(GenerationMediaArtifact).where(GenerationMediaArtifact.attempt_id == attempt)
            )
        )
        assert (artifact.id, artifact.file_id, artifact.media_json) == (
            artifact_id,
            file_id,
            original_media,
        )
        assert version.metadata_json["video_source"]["artifact_id"] == str(artifact.id)
        assert version.metadata_json["video_source"]["source_file_id"] == str(file_id)


def test_parallel_video_replacements_share_one_asset_revision_and_one_winner(
    client, settings, postgres_video_case
):
    case = postgres_video_case
    data = video_bytes()
    job, attempt, provider = finish_video(client, settings, case, data)
    factory = client.app.state.session_factory
    assert generation_artifacts.import_attempt(factory, settings, attempt, provider) == 1
    root = f"/api/v1/projects/{case['project_id']}"
    candidates_url = root + f"/generation-jobs/{job['id']}/candidates"
    response = client.get(candidates_url)
    assert response.status_code == 200, response.text
    [source] = response.json()
    file_id = manual_file(client, settings, case["project_id"], data)
    payloads = [
        {
            "file_id": file_id,
            "creation_key": str(uuid4()),
            "expected_revision": source["asset_revision"],
        }
        for _ in range(2)
    ]
    barrier = Barrier(2)

    def replace(payload):
        barrier.wait(timeout=30)
        return client.post(
            root + f"/generation-candidates/{source['id']}/replacements", json=payload
        )

    # Each real request opens its own transaction; neither permissions, project
    # locking nor the AssetVersion append/CAS path is mocked.
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(replace, payloads))
    assert sorted(result.status_code for result in responses) == [201, 409]
    [winner] = [result.json() for result in responses if result.status_code == 201]
    [loser] = [result.json() for result in responses if result.status_code == 409]
    assert loser["error"]["code"] == "VERSION_CONFLICT"
    assert winner["replaces_candidate_id"] == source["id"]
    assert winner["asset_revision"] == source["asset_revision"] + 1
    assert winner["version"]["status"] == "DRAFT"
    assert winner["input_manifest_id"] == source["input_manifest_id"]
    assert len(client.get(candidates_url).json()) == 2
    original = client.get(root + f"/generation-candidates/{source['id']}")
    assert original.status_code == 200, original.text
    assert original.json()["version"] == source["version"]
    with factory() as db:
        asset = db.get(VisualAsset, UUID(source["version"]["asset_id"]))
        assert asset is not None and asset.current_version_id is None
        assert asset.revision == source["asset_revision"] + 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AssetVersion)
                .where(AssetVersion.asset_id == asset.id)
            )
            == 2
        )
