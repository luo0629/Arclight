import base64
import hashlib
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from arclight.persistence.models import AuditEvent, ProjectMember, StorySource


def source_payload(raw=b"A story\r\n", **changes):
    return {
        "import_key": str(uuid4()),
        "original_name": "故事.txt",
        "media_type": "text/plain",
        "encoding": "utf-8",
        "content_base64": base64.b64encode(raw).decode(),
        "source_note": "本人原创初稿",
        "rights_note": "本人拥有使用权",
        "rights_confirmed": True,
        **changes,
    }


def sources_path(episode):
    return f"/api/v1/episodes/{episode['id']}/story-sources"


def test_import_preserves_exact_original_and_provenance_without_starting_run(client, episode):
    path = sources_path(episode)
    raw = b"\xef\xbb\xbf" + "# 雨夜\r\n\r\n<script>alert('x')</script>  \r\n".encode()
    payload = source_payload(raw, original_name="故事.MD", media_type="text/markdown")
    response = client.post(path, json=payload)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["text"] == raw.decode("utf-8-sig")
    assert row["encoding"] == "utf-8-sig"
    assert row["source_type"] == "MARKDOWN"
    assert row["byte_size"] == len(raw)
    assert row["sha256"] == hashlib.sha256(raw).hexdigest()
    assert row["source_note"] == payload["source_note"]
    assert row["rights_note"] == payload["rights_note"]
    assert row["created_by"] == "service-owner"
    assert row["import_no"] == 1
    assert "raw_bytes" not in row and "content_base64" not in row
    listed = client.get(path).json()
    assert len(listed) == 1 and "text" not in listed[0]
    assert client.get(path + "/" + row["id"]).json() == row
    original = client.get(path + "/" + row["id"] + "/original")
    assert original.content == raw
    assert original.headers["content-type"] == "application/octet-stream"
    assert original.headers["x-content-type-options"] == "nosniff"
    assert original.headers["cache-control"] == "no-store"
    assert original.headers["content-disposition"].startswith("attachment;")
    assert client.get(f"/api/v1/episodes/{episode['id']}/runs").json() == []
    with client.app.state.session_factory() as db:
        audits = list(
            db.scalars(select(AuditEvent).where(AuditEvent.action == "story_source.imported"))
        )
        assert len(audits) == 1
        assert audits[0].details_json["sha256"] == row["sha256"]
        assert audits[0].details_json["rights_confirmed"] is True
        assert "text" not in audits[0].details_json


def test_explicit_gb18030_and_exact_size_boundary(client, episode):
    path = sources_path(episode)
    raw = "旧版中文故事：雨夜书店。".encode("gb18030")
    assert client.post(path, json=source_payload(raw)).json()["error"]["code"] == "SOURCE_ENCODING"
    response = client.post(path, json=source_payload(raw, encoding="gb18030"))
    assert response.status_code == 201, response.text
    assert response.json()["text"] == raw.decode("gb18030")
    assert response.json()["encoding"] == "gb18030"
    boundary = client.post(path, json=source_payload(b"a" * 262144))
    assert boundary.status_code == 201, boundary.text
    assert boundary.json()["byte_size"] == 262144
    too_large = client.post(path, json=source_payload(b"a" * 262145))
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "SOURCE_TOO_LARGE"


@pytest.mark.parametrize(
    "raw,changes,code",
    [
        (b"", {}, "SOURCE_EMPTY"),
        (b" \t\r\n", {}, "SOURCE_EMPTY"),
        (b"hello\x00world", {}, "SOURCE_NOT_TEXT"),
        (b"hello\x1bworld", {}, "SOURCE_NOT_TEXT"),
        (b"%PDF-1.7 text", {}, "SOURCE_NOT_TEXT"),
        (b"PK\x03\x04text", {}, "SOURCE_NOT_TEXT"),
        ("损坏�".encode(), {}, "SOURCE_NOT_TEXT"),
        ("故事".encode("utf-16"), {}, "SOURCE_ENCODING"),
        (b"text", {"original_name": "story.docx"}, "SOURCE_TYPE_UNSUPPORTED"),
        (b"text", {"original_name": "story.txt.exe"}, "SOURCE_TYPE_UNSUPPORTED"),
        (b"text", {"original_name": "../story.txt"}, "INVALID_SOURCE_NAME"),
        (b"text", {"original_name": "C:\\story.txt"}, "INVALID_SOURCE_NAME"),
        (b"text", {"original_name": "story\x00.txt"}, "INVALID_SOURCE_NAME"),
        (b"text", {"media_type": "application/pdf"}, "SOURCE_TYPE_UNSUPPORTED"),
        (b"text", {"content_base64": "not base64!"}, "INVALID_SOURCE_DATA"),
    ],
)
def test_invalid_files_are_rejected_atomically(client, episode, raw, changes, code):
    path = sources_path(episode)
    response = client.post(path, json=source_payload(raw, **changes))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert client.get(path).json() == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(StorySource)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "story_source.imported")
            )
            == 0
        )


def test_authorization_metadata_and_input_contract_are_required(client, episode):
    path = sources_path(episode)
    for changes in [
        {"rights_confirmed": False},
        {"rights_note": "  "},
        {"source_note": "  "},
        {"encoding": "auto"},
        {"encoding": "utf-16"},
        {"extra": "forbidden"},
    ]:
        assert client.post(path, json=source_payload(**changes)).status_code == 422
    payload = source_payload()
    del payload["rights_confirmed"]
    assert client.post(path, json=payload).status_code == 422
    assert client.get(path).json() == []


def test_retry_is_idempotent_and_each_new_import_is_independent(client, episode):
    path = sources_path(episode)
    payload = source_payload()
    first = client.post(path, json=payload).json()
    assert client.post(path, json=payload).json() == first
    assert client.post(path, json={**payload, "rights_note": "changed"}).status_code == 409
    assert client.post(path, json={**payload, "content_base64": "Y2hhbmdlZA=="}).status_code == 409
    second = client.post(path, json=source_payload(b"another story")).json()
    assert second["import_no"] == 2 and second["id"] != first["id"]
    assert client.get(path + "?limit=1&offset=0").json()[0]["id"] == second["id"]
    assert client.get(path + "?limit=1&offset=1").json()[0]["id"] == first["id"]
    assert client.get(path + "?offset=2").json() == []
    assert client.get(path + "?limit=101").status_code == 422
    assert client.get(path + "?offset=-1").status_code == 422
    assert client.put(path + "/" + first["id"], json={"text": "changed"}).status_code == 405
    assert client.delete(path + "/" + first["id"]).status_code == 405
    assert client.get(path + "/" + first["id"]).json() == first
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "story_source.imported")
            )
            == 2
        )


def test_source_scope_archive_deletion_and_restore(client, episode):
    path = sources_path(episode)
    row = client.post(path, json=source_payload()).json()
    original = path + "/" + row["id"] + "/original"
    project_path = f"/api/v1/projects/{episode['project_id']}"
    project = client.get(project_path).json()
    other = client.post("/api/v1/projects", json={"name": "Other"}).json()
    other_episode = client.post(
        f"/api/v1/projects/{other['id']}/episodes", json={"episode_no": 1, "title": "other"}
    ).json()
    assert client.get(sources_path(other_episode) + "/" + row["id"]).status_code == 404
    assert (
        client.get(sources_path(other_episode) + "/" + row["id"] + "/original").status_code == 404
    )
    assert (
        client.patch(f"/api/v1/episodes/{episode['id']}", json={"archived": True}).status_code
        == 200
    )
    assert client.post(path, json=source_payload()).status_code == 409
    assert client.get(original).status_code == 200
    assert (
        client.patch(f"/api/v1/episodes/{episode['id']}", json={"archived": False}).status_code
        == 200
    )
    assert client.patch(project_path, json={"archived": True}).status_code == 200
    assert client.post(path, json=source_payload()).status_code == 409
    deletion = client.request("DELETE", project_path, json={"confirm_name": project["name"]}).json()
    for url in [path, path + "/" + row["id"], original]:
        assert client.get(url).status_code == 404
    assert client.post(path, json=source_payload()).status_code == 404
    assert (
        client.post(
            project_path + "/restore", json={"deletion_id": deletion["deletion_id"]}
        ).status_code
        == 200
    )
    assert client.get(path + "/" + row["id"]).json() == row
    assert client.get(original).content == b"A story\r\n"


def test_owner_editor_viewer_and_outsider_access(client):
    from test_project_deletion import create_episode, create_project, register

    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    path = sources_path(episode)
    row = client.post(path, json=source_payload()).json()
    member = register(client, "source-member@example.com")
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role="viewer"
        )
        db.add(membership)
    assert client.get(path).status_code == 200
    assert client.get(path + "/" + row["id"]).status_code == 200
    assert client.get(path + "/" + row["id"] + "/original").status_code == 200
    assert client.post(path, json=source_payload()).status_code == 403
    with client.app.state.session_factory.begin() as db:
        db.scalar(
            select(ProjectMember).where(ProjectMember.user_id == UUID(member["user"]["id"]))
        ).role = "editor"
    assert client.post(path, json=source_payload()).status_code == 201
    register(client, "source-outsider@example.com")
    assert client.get(path).status_code == 404
    assert client.get(path + "/" + row["id"]).status_code == 404
    assert client.get(path + "/" + row["id"] + "/original").status_code == 404
    assert client.post(path, json=source_payload()).status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert len(client.get(path).json()) == 2
    client.headers.clear()
    assert client.get(path).status_code == 401
    assert client.post(path, json=source_payload()).status_code == 401


@pytest.mark.legacy_schema
def test_migration_roundtrip_and_nonempty_downgrade_protection(client, episode, migrated):
    command.downgrade(migrated, "0006")
    command.upgrade(migrated, "0018")
    row = client.post(sources_path(episode), json=source_payload()).json()
    with pytest.raises(RuntimeError, match="Source provenance exists"):
        command.downgrade(migrated, "0006")
    assert client.get(sources_path(episode) + "/" + row["id"]).json() == row


def test_offline_source_downgrade_requires_online_check():
    from pathlib import Path

    config = Config(
        str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=StringIO()
    )
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    with pytest.raises(RuntimeError, match="online source check"):
        command.downgrade(config, "0007:0006", sql=True)


@pytest.mark.parametrize("confirmation", [1, "true", "yes", None])
def test_rights_confirmation_requires_json_true(client, episode, confirmation):
    response = client.post(
        sources_path(episode), json=source_payload(rights_confirmed=confirmation)
    )
    assert response.status_code == 422
    assert client.get(sources_path(episode)).json() == []
