"""ASSET-03 typed profiles, exact references and project/CAS protection."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from test_assets import archive, create, path, rename
from test_collaboration import create_project, register
from test_text_contract_v2 import make_asset

from arclight.persistence.models import AssetVersion, ProjectMember, VisualAsset


def save_profile(client, row, profile, **extra):
    return client.put(
        path(row["project_id"]) + "/" + row["id"] + "/character-profile",
        json={"profile": profile, "expected_revision": row["revision"], **extra},
    )


def profile(reference):
    outfit, initial, angle = (str(uuid4()) for _ in range(3))
    return {
        "fixed_features": ["黑发", "圆脸"],
        "variable_features": ["姿势"],
        "expressions": ["笑", "疑惑"],
        "outfits": [{"id": outfit, "name": "常服", "description": "蓝衣"}],
        "default_outfit_id": outfit,
        "images": [{"id": initial, "label": "设定", "kind": "INITIAL", "source": reference}],
        "standard_image_id": initial,
    }


def test_profile_references_are_exact_and_do_not_select_media(client, episode):
    row = create(client, episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, episode["project_id"], "IMAGE")
        second = AssetVersion(
            asset_id=UUID(reference["asset_id"]),
            version_no=2,
            object_key="fixture/newer",
            checksum="b" * 64,
            created_by="service-owner",
            status="APPROVED",
        )
        db.add(second)
    data = profile(reference)
    changed = save_profile(client, row, data)
    assert changed.status_code == 200, changed.text
    saved = changed.json()
    assert saved["character_profile"]["images"][0]["source"] == reference
    assert saved["current_version_id"] is None and saved["lock_version"] == 0
    assert saved["revision"] == 1 and saved["status"] == "DRAFT"
    assert rename(client, row).status_code == 409
    assert save_profile(client, row, data).status_code == 409
    refs = client.get(f"/api/v1/projects/{row['project_id']}/reference-images?limit=1").json()
    assert len(refs) == 1
    next_refs = client.get(
        f"/api/v1/projects/{row['project_id']}/reference-images?limit=1&offset=1"
    ).json()
    assert {refs[0]["version_no"], next_refs[0]["version_no"]} == {1, 2}
    assert refs[0]["id"] != next_refs[0]["id"]
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json() == saved
    archived = archive(client, saved).json()
    assert archived["character_profile"] == saved["character_profile"]
    assert save_profile(client, archived, data).status_code == 409


@pytest.mark.parametrize(
    "bad",
    [
        {"fixed_features": ["黑发"], "variable_features": [" 黑发 "]},
        {"fixed_features": [" "]},
        {"expressions": ["笑", "笑"]},
        {"fixed_features": ["x" * 81]},
        {"variable_features": [str(i) for i in range(31)]},
        {"default_outfit_id": str(uuid4())},
        {"standard_image_id": str(uuid4())},
        {"outfits": [{"id": str(uuid4()), "name": " "}]},
        {"freeform": {}},
    ],
)
def test_profile_validation_is_atomic(client, episode, bad):
    row = create(client, episode["project_id"])
    assert save_profile(client, row, bad).status_code == 422
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json() == row


@pytest.mark.parametrize(
    "state",
    [
        "DRAFT",
        "READY_FOR_REVIEW",
        "REJECTED",
        "STALE",
        "ARCHIVED",
        "stale_flag",
        "foreign",
        "wrong_type",
        "wrong_identity",
    ],
)
def test_standard_reference_validation(client, episode, state):
    row = create(client, episode["project_id"])
    other = create_project(client) if state == "foreign" else None
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(
            db,
            other["id"] if other else episode["project_id"],
            "PROP" if state == "wrong_type" else "IMAGE",
        )
        version = db.get(AssetVersion, UUID(reference["asset_version_id"]))
        if state in {"DRAFT", "READY_FOR_REVIEW", "REJECTED", "STALE", "ARCHIVED"}:
            version.status = state
        if state == "stale_flag":
            version.stale = True
        if state == "wrong_identity":
            reference["asset_id"] = row["id"]
    assert save_profile(client, row, profile(reference)).status_code == 422
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json() == row


def test_candidate_angle_and_manual_standard_choice(client, episode):
    row = create(client, episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        initial = make_asset(db, episode["project_id"], "IMAGE")
        angle = make_asset(db, episode["project_id"], "IMAGE")
        db.get(AssetVersion, UUID(angle["asset_version_id"])).status = "READY_FOR_REVIEW"
    data = profile(initial)
    angle_row = {
        "id": str(uuid4()),
        "label": "侧面",
        "kind": "ANGLE",
        "angle": "左侧",
        "source": angle,
    }
    data["images"].append(angle_row)
    response = save_profile(client, row, data)
    assert response.status_code == 200, response.text
    data["standard_image_id"] = angle_row["id"]
    assert save_profile(client, response.json(), data).status_code == 422
    data["standard_image_id"] = None
    assert save_profile(client, response.json(), data).status_code == 200
    data["images"][1]["angle"] = ""
    assert save_profile(client, row, data).status_code == 422


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_character_profile_permissions(client, role):
    register(client)
    project = create_project(client)
    row = create(client, project["id"])
    member = register(client, f"profile-{role}@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    expected = {"editor": 200, "viewer": 403, "outsider": 404}[role]
    assert save_profile(client, row, {"fixed_features": ["黑发"]}).status_code == expected
    assert client.get(f"/api/v1/projects/{project['id']}/reference-images").status_code == (
        404 if role == "outsider" else 200
    )


def test_character_only_and_locked_gates(client, episode):
    row = create(client, episode["project_id"], "PROP")
    assert save_profile(client, row, {}).status_code == 422
    with client.app.state.session_factory.begin() as db:
        asset = db.get(VisualAsset, UUID(row["id"]))
        asset.asset_type = "CHARACTER"
        asset.status = "LOCKED"
    assert save_profile(client, row, {}).status_code == 409


def test_character_profile_migration_protection(client, episode, migrated):
    row = create(client, episode["project_id"])
    command.downgrade(migrated, "0013")
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json() == row
    assert save_profile(client, row, {"expressions": ["笑"]}).status_code == 200
    with pytest.raises(RuntimeError, match="Character profiles exist"):
        command.downgrade(migrated, "0013")
    config = Config(migrated.config_file_name, output_buffer=StringIO())
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0014:0013", sql=True)


def test_concurrent_profile_and_metadata_cas(client, episode, settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL locking verified in dedicated CI database")
    row = create(client, episode["project_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(save_profile, client, row, {"expressions": ["笑"]}),
            pool.submit(rename, client, row),
        ]
        assert sorted(call.result().status_code for call in calls) == [200, 409]
