"""GEN-03: exact media inputs, generic targets, authorization and secret boundaries."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_asset_selection import approved, current
from test_asset_versions import private_objects, save  # noqa: F401
from test_assets import create, rename
from test_collaboration import create_project, register

from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    DependencyManifest,
    GenerationAttempt,
    GenerationJob,
    ProjectMember,
)


def base(project_id):
    return f"/api/v1/projects/{project_id}/generation-jobs"


def payload(target=None, **changes):
    values = {
        "target_type": "SHOT_IMAGE",
        "task_type": "IMAGE",
        "spec": {
            "model": "mock-image-v1",
            "model_config_version": "preset:7",
            "prompt": "雨夜中的街道",
            "prompt_version": "template:3",
            "negative_prompt": "模糊",
            "parameters": {"steps": 12},
            "output_spec": {"width": 960, "height": 540, "format": "png"},
            "context": {"summary": "主角从车站回家", "builder_version": "1"},
            "seed": 17,
            "variation": "explore-2",
        },
    }
    if target is not None:
        values.update(
            {
                "target_type": target["asset_type"],
                "target_asset_id": target["id"],
                "expected_asset": {
                    name: target[name]
                    for name in (
                        "revision",
                        "lock_version",
                        "current_version_id",
                    )
                },
            }
        )
    return values | changes


def capture(client, project_id, body=None, key=None):
    return client.post(
        base(project_id),
        json=body or payload(),
        headers={
            "Idempotency-Key": key or str(uuid4()),
        },
    )


def capture_ok(client, project_id, body=None):
    response = capture(client, project_id, body)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("kind", ["CHARACTER", "SCENE", "SHOT_IMAGE", "AUDIO", "RENDER"])
def test_generic_jobs_freeze_complete_inputs_without_placeholder_assets(client, episode, kind):
    project_id = episode["project_id"]
    target = create(client, project_id, kind) if kind in {"CHARACTER", "SCENE"} else None
    body = payload(
        target,
        target_type=kind,
        task_type=kind if kind in {"AUDIO", "RENDER"} else "IMAGE",
        episode_id=episode["id"],
    )
    row = capture_ok(client, project_id, body)
    assert row["status"] == "CREATED" and row["attempt_count"] == 0
    assert row["target_asset_id"] == (target["id"] if target else None)
    assert client.get(base(project_id) + "/" + row["id"]).json() == row
    assert client.get(base(project_id) + "/" + row["id"] + "/attempts").json() == []
    with client.app.state.session_factory() as db:
        manifest = db.get(DependencyManifest, UUID(row["input_manifest_id"]))
        spec = manifest.snapshot_json["context"]["generation"]
        if body["task_type"] == "IMAGE":
            assert spec["prompt"] == (
                "【画面描述】\n画面要求：雨夜中的街道\n\n【连续性上下文】\n剧情摘要：主角从车站回家"
            )
            assert spec["negative_prompt"] == body["spec"]["negative_prompt"]
            composition = manifest.snapshot_json["context"]["prompt_composition"]
            assert composition["composer_version"] == "1"
            assert composition["provider_parameters"]["parameters"] == spec["parameters"]
        else:
            assert spec == body["spec"] | {
                "schema_version": 1,
                "provider": "mock",
                "batch_size": 1,
                "require_query": False,
                "require_cancel": False,
            }
        assert manifest.target_asset_id == (UUID(target["id"]) if target else None)
        assert manifest.snapshot_json["target"]["expected_asset"] == body.get("expected_asset")
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 0
        assert db.scalar(select(GenerationJob.created_by)) == "service-owner"


def test_exact_reference_and_config_remain_frozen_after_source_and_request_changes(client, episode):
    project_id = episode["project_id"]
    source = create(client, project_id)
    version = approved(client, source)
    body = payload(
        inputs=[
            {
                "source_asset_version_id": version["id"],
                "dependency_type": "REFERENCE_DEPENDENCY",
                "selectors": ["face.front"],
            }
        ]
    )
    row = capture_ok(client, project_id, body)
    manifest_url = f"/api/v1/projects/{project_id}/dependency-manifests/{row['input_manifest_id']}"
    before = client.get(manifest_url).json()
    assert before["snapshot"]["inputs"][0] == body["inputs"][0] | {
        "asset_id": source["id"],
        "asset_type": "CHARACTER",
        "asset_name": source["name"],
        "version_no": version["version_no"],
        "checksum": version["checksum"],
    }
    assert rename(client, current(client, source), "新名字").status_code == 200
    approved(client, current(client, source))
    body["spec"]["prompt"] = "新提示词"
    body["spec"]["parameters"]["steps"] = 99
    assert client.get(manifest_url).json() == before
    with client.app.state.session_factory() as db:
        audits = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "generation.created")
        ).all()
        assert len(audits) == 1
        assert "prompt" not in str(audits[0].details_json)
        assert db.scalar(select(func.count()).select_from(AssetVersion)) == 2


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_create_and_attempt_reads_enforce_membership(client, role):
    register(client)
    project = create_project(client)
    row = capture_ok(client, project["id"])
    member = register(client, f"gen-{role}@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    url = base(project["id"]) + "/" + row["id"]
    assert (
        capture(client, project["id"]).status_code
        == {"editor": 201, "viewer": 403, "outsider": 404}[role]
    )
    expected = 404 if role == "outsider" else 200
    assert client.get(url + "/attempts").status_code == expected
    assert (
        client.post(url + "/retry", json={"expected_revision": 0}).status_code
        == {"editor": 409, "viewer": 403, "outsider": 404}[role]
    )
    foreign = create_project(client)
    assert client.get(url.replace(project["id"], foreign["id"]) + "/attempts").status_code == 404


@pytest.mark.parametrize(
    "bad",
    ["target_cas", "pointer", "locked", "archived", "episode", "project", "foreign_target", "type"],
)
def test_invalid_target_has_no_partial_job_or_manifest(client, episode, bad):
    project_id = episode["project_id"]
    target = create(client, project_id)
    body = payload(target, episode_id=episode["id"])
    if bad == "target_cas":
        rename(client, target)
    elif bad == "pointer":
        body["expected_asset"]["current_version_id"] = str(uuid4())
    elif bad in {"locked", "archived"}:
        from arclight.persistence.models import VisualAsset

        with client.app.state.session_factory.begin() as db:
            db.get(VisualAsset, UUID(target["id"])).status = bad.upper()
    elif bad == "episode":
        other = create_project(client)
        body["episode_id"] = client.post(
            f"/api/v1/projects/{other['id']}/episodes", json={"episode_no": 1, "title": "另一集"}
        ).json()["id"]
    elif bad == "project":
        client.patch(f"/api/v1/projects/{project_id}", json={"archived": True})
    elif bad == "foreign_target":
        project_id = create_project(client)["id"]
    else:
        body["target_type"] = "SCENE"
    assert capture(client, project_id, body).status_code in {404, 409, 422}
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.scalar(select(func.count()).select_from(DependencyManifest)) == 0


@pytest.mark.parametrize("bad", ["draft", "stale", "foreign", "type", "duplicate", "selectors"])
def test_invalid_inputs_are_rejected_atomically(client, episode, bad):
    project_id = episode["project_id"]
    source = create(client, create_project(client)["id"] if bad == "foreign" else project_id)
    version = save(client, source) if bad == "draft" else approved(client, source)
    if bad == "stale":
        with client.app.state.session_factory.begin() as db:
            db.get(AssetVersion, UUID(version["id"])).stale = True
    item = {"source_asset_version_id": version["id"], "dependency_type": "REFERENCE_DEPENDENCY"}
    if bad == "type":
        item["dependency_type"] = "SEQUENCE_DEPENDENCY"
    if bad == "selectors":
        item["selectors"] = ["face", "face"]
    body = payload(inputs=[item, deepcopy(item)] if bad == "duplicate" else [item])
    assert capture(client, project_id, body).status_code in {404, 409, 422}
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.scalar(select(func.count()).select_from(DependencyManifest)) == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"api_key": "private-canary-43"},
        {"nested": {"Authorization": "private-canary-43"}},
        {"image_base64": "private-canary-43"},
        {"url": "https://host/x?X-Amz-Signature=private-canary-43"},
        {"url": "https://user:private-canary-43@host/x"},
        {"text": "Bearer private-canary-43"},
    ],
)
def test_credentials_and_inline_media_never_reach_history_or_error_logs(
    client, episode, caplog, bad
):
    body = payload()
    body["spec"]["context"] = bad
    response = capture(client, episode["project_id"], body)
    assert response.status_code == 422
    assert "private-canary-43" not in response.text + caplog.text
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(DependencyManifest)) == 0
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "paid-provider"),
        ("seed", True),
        ("seed", -1),
        ("parameters", {"seed": 7}),
        ("prompt", " "),
        ("context", {"long": "x" * 65536}),
        ("prompt", "Bearer private-canary-43"),
    ],
)
def test_media_spec_bounds_and_mock_scope(client, episode, field, value):
    body = payload()
    body["spec"][field] = value
    assert capture(client, episode["project_id"], body).status_code == 422


def test_creation_requires_bounded_header_and_explicit_target_expectations(client, episode):
    url = base(episode["project_id"])
    for header in ({}, {"Idempotency-Key": "x" * 161}, {"Idempotency-Key": "a b"}):
        assert client.post(url, json=payload(), headers=header).status_code == 422
    body = payload(create(client, episode["project_id"]))
    del body["expected_asset"]
    assert capture(client, episode["project_id"], body).status_code == 422
    client.headers.pop("Authorization")
    assert capture(client, episode["project_id"]).status_code == 401
