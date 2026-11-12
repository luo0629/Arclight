"""Exercise the public upload and audio-import flow against isolated S3 in CI."""

import hashlib
from uuid import UUID, uuid4

from sqlalchemy import select
from test_audio_import import import_request
from test_audio_production import audio_source, wave_bytes
from test_file_transfers import transfer_setup, upload_payload, upload_to_s3
from test_upload_completion import DECLARATION

from arclight.persistence.models import AssetVersion, StoredFile, VisualAsset
from arclight.storage.objects import ObjectStorage

__all__ = ["audio_source", "transfer_setup"]


def test_public_completed_audio_upload_imports_into_a_stable_line_target(
    client, audio_source, transfer_setup
):
    settings, root = transfer_setup
    data = wave_bytes()
    response = client.post(
        root + "/uploads",
        json=upload_payload(
            purpose="AUDIO",
            original_name="original-recording.wav",
            media_type="audio/wav",
            byte_size=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
        ),
    )
    assert response.status_code == 201, response.text
    ticket = response.json()
    assert upload_to_s3(ticket, data=data).status_code == 204
    completed = client.post(root + f"/uploads/{ticket['upload_id']}/complete", json=DECLARATION)
    assert completed.status_code == 200, completed.text
    candidate = completed.json()
    assert candidate["stage"] == "CANDIDATE"
    url, payload = import_request(audio_source, None, candidate["id"])
    del payload["target_asset_id"]
    imported = client.post(url, json=payload)
    assert imported.status_code == 201, imported.text
    first = imported.json()
    assert first["source_file_id"] == candidate["id"]
    assert first["validation"]["sample_rate"] == 24000
    assert not first["validation"]["silence"]["silent"]
    repeated = client.post(url, json=payload)
    assert repeated.status_code == 200 and repeated.json() == first
    # Overwriting the temporary upload must not change the frozen import source.
    assert upload_to_s3(ticket, data=b"x" * len(data)).status_code == 204
    second = client.post(
        url,
        json={
            **payload,
            "creation_key": str(uuid4()),
            "expected_revision": first["asset_revision"],
        },
    )
    assert second.status_code == 201, second.text
    result = second.json()
    assert result["target_asset_id"] == first["target_asset_id"]
    assert result["asset_version_id"] != first["asset_version_id"]
    with client.app.state.session_factory() as db:
        original = db.get(StoredFile, UUID(candidate["id"]))
        assert original.asset_version_id is None
        output = db.get(StoredFile, UUID(result["file_id"]))
        assert output.storage_backend == "S3" and output.bucket == settings.s3_bucket
        assert ObjectStorage(settings).read(output.object_key, len(data), output.checksum) == data
        assert (
            ObjectStorage(settings).read(original.object_key, len(data), original.checksum) == data
        )
        versions = list(
            db.scalars(
                select(AssetVersion)
                .where(AssetVersion.asset_id == UUID(first["target_asset_id"]))
                .order_by(AssetVersion.version_no)
            )
        )
        assert [row.version_no for row in versions] == [1, 2]
        assert all(row.status == "DRAFT" and not row.stale for row in versions)
        target = db.get(VisualAsset, UUID(first["target_asset_id"]))
        assert target.asset_type == "AUDIO" and target.current_version_id is None
