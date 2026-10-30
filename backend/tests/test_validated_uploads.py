import hashlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_file_transfers import transfer_setup, upload_payload, upload_to_s3
from test_media_validation import compressed_audio, font_bytes, wave_bytes
from test_upload_completion import DECLARATION

from arclight.persistence.models import StoredFile, UploadCompletion
from arclight.storage.objects import ObjectStorage

__all__ = ["transfer_setup"]


@pytest.mark.parametrize(
    "media_type,name,purpose",
    [
        ("text/plain", "story.txt", "STORY"),
        ("text/markdown", "story.md", "STORY"),
        ("audio/wav", "voice.wav", "AUDIO"),
        ("audio/mpeg", "music.mp3", "MUSIC"),
        ("audio/ogg", "music.ogg", "MUSIC"),
        ("font/ttf", "font.ttf", "FONT"),
        ("font/otf", "font.otf", "FONT"),
    ],
)
def test_validated_media_keeps_exact_private_bytes(
    client, transfer_setup, media_type, name, purpose
):
    settings, root = transfer_setup
    if media_type.startswith("text/"):
        data = "原始故事\n".encode()
    elif media_type == "audio/wav":
        data = wave_bytes()
    elif media_type.startswith("audio/"):
        data = compressed_audio(media_type)
    else:
        data = font_bytes(media_type == "font/ttf")
    payload = upload_payload(
        purpose=purpose,
        original_name=name,
        media_type=media_type,
        byte_size=len(data),
        checksum=hashlib.sha256(data).hexdigest(),
    )
    response = client.post(root + "/uploads", json=payload)
    assert response.status_code == 201, response.text
    upload = response.json()
    assert upload_to_s3(upload, data=data).status_code == 204
    path = root + f"/uploads/{upload['upload_id']}/complete"
    result = client.post(path, json=DECLARATION)
    assert result.status_code == 200, result.text
    receipt = result.json()
    assert receipt["stage"] == "CANDIDATE" and receipt["media_type"] == media_type
    assert receipt["byte_size"] == len(data) and receipt["checksum"] == payload["checksum"]
    assert client.post(path, json=DECLARATION).json() == receipt
    assert upload_to_s3(upload, data=b"x" * len(data)).status_code == 204
    with client.app.state.session_factory() as db:
        row = db.get(StoredFile, UUID(receipt["id"]))
        assert row.object_key != upload["fields"]["key"]
        assert ObjectStorage(settings).read(row.object_key, len(data), row.checksum) == data
    assert (
        client.post(
            root + f"/files/{receipt['id']}/asset-versions",
            json={"asset_id": str(uuid4()), "creation_key": str(uuid4()), "expected_revision": 0},
        ).status_code
        == 409
    )


def test_bad_actual_media_never_produces_validated_metadata(client, transfer_setup):
    settings, root = transfer_setup
    data = b"RIFF" + bytes(80)
    upload = client.post(
        root + "/uploads",
        json=upload_payload(
            purpose="AUDIO",
            original_name="voice.wav",
            media_type="audio/wav",
            byte_size=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
        ),
    ).json()
    assert upload_to_s3(upload, data=data).status_code == 204
    assert (
        client.post(root + f"/uploads/{upload['upload_id']}/complete", json=DECLARATION).status_code
        == 422
    )
    assert client.get(root + f"/uploads/{upload['upload_id']}").json()["stage"] == "TEMPORARY"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(StoredFile)) is None
        assert db.scalar(select(UploadCompletion)) is None
    objects = ObjectStorage(settings).client.list_objects_v2(Bucket=settings.s3_bucket)
    assert objects["KeyCount"] == 1 and objects["Contents"][0]["Key"].startswith("temporary/")
