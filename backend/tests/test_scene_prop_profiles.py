"""ASSET-04 scene/prop maintenance and exact reuse across real saved shots."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from test_assets import archive, create, path, rename
from test_collaboration import create_project, register
from test_text_contract_v2 import board_content, make_asset, save_board, save_script

from arclight.persistence.models import AssetVersion, ProjectMember, VisualAsset


def save_profile(client, row, profile, kind="scene", **extra):
    return client.put(
        path(row["project_id"]) + "/" + row["id"] + "/" + kind + "-profile",
        json={"profile": profile, "expected_revision": row["revision"], **extra},
    )


def test_scene_reference_reused_across_shots_and_not_retargeted(client, episode):
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, episode["project_id"], "SCENE")
    row = client.get(path(episode["project_id"]) + "/" + reference["asset_id"]).json()
    data = {
        "layout": "东侧窗，西侧门",
        "time_of_day": "雨夜",
        "lighting": "暖台灯",
        "palette": "蓝灰",
        "environment": "安静书房",
        "reference": reference,
    }
    result = save_profile(client, row, data)
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved["scene_profile"] == data
    script = save_script(client, episode)
    content = board_content(script)
    first = content["shots"][0]
    first["duration_ms"] = 15000
    first["scene_asset"] = reference
    second = deepcopy(first)
    second["shot_no"] = 2
    content["shots"].append(second)
    board = save_board(client, episode, script, content)
    assert board.status_code == 201, board.text
    assert [shot["scene_asset"] for shot in board.json()["content_json"]["shots"]] == [
        reference,
        reference,
    ]
    with client.app.state.session_factory.begin() as db:
        db.add(
            AssetVersion(
                asset_id=UUID(reference["asset_id"]),
                version_no=2,
                object_key="fixture/new-scene",
                checksum="b" * 64,
                created_by="service-owner",
                status="APPROVED",
            )
        )
    updated = save_profile(client, saved, {**data, "lighting": "冷光"})
    assert updated.status_code == 200
    assert updated.json()["scene_profile"]["reference"] == reference
    archived = archive(client, updated.json()).json()
    assert archived["scene_profile"]["reference"] == reference
    boards = client.get(f"/api/v1/episodes/{episode['id']}/storyboards").json()
    assert [shot["scene_asset"] for shot in boards[0]["content_json"]["shots"]] == [
        reference,
        reference,
    ]
    assert save_profile(client, archived, data).status_code == 409


def test_prop_holder_exact_version_and_clear(client, episode):
    row = create(client, episode["project_id"], "PROP")
    with client.app.state.session_factory.begin() as db:
        holder = make_asset(db, episode["project_id"], "CHARACTER")
    data = {"appearance": "磨损铜钥匙", "holder": holder, "story_state": "交给主角，缺一齿"}
    result = save_profile(client, row, data, "prop")
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved["prop_profile"] == data
    assert saved["current_version_id"] is None and saved["lock_version"] == 0
    assert rename(client, row).status_code == 409
    assert save_profile(client, row, data, "prop").status_code == 409
    cleared = save_profile(client, saved, {**data, "holder": None}, "prop")
    assert cleared.status_code == 200 and cleared.json()["prop_profile"]["holder"] is None


@pytest.mark.parametrize("kind", ["scene", "prop"])
@pytest.mark.parametrize(
    "problem", ["foreign", "wrong_type", "wrong_identity", "missing", "draft", "archived", "stale"]
)
def test_exact_profile_references_reject_invalid_atomically(client, episode, kind, problem):
    row = create(client, episode["project_id"], "SCENE" if kind == "scene" else "PROP")
    other = create_project(client) if problem == "foreign" else None
    with client.app.state.session_factory.begin() as db:
        ref = make_asset(
            db,
            other["id"] if other else episode["project_id"],
            "CHARACTER" if kind == "prop" else "SCENE",
        )
        version = db.get(AssetVersion, UUID(ref["asset_version_id"]))
        if kind == "scene" and problem not in {"foreign", "wrong_identity"}:
            version.asset_id = UUID(row["id"])
            ref["asset_id"] = row["id"]
        if problem == "wrong_type":
            db.get(VisualAsset, UUID(ref["asset_id"])).asset_type = "STYLE"
        elif problem == "wrong_identity":
            ref["asset_id"] = str(uuid4())
        elif problem == "missing":
            ref["asset_version_id"] = str(uuid4())
        elif problem == "draft":
            version.status = "DRAFT"
        elif problem == "archived":
            version.status = "ARCHIVED"
        elif problem == "stale":
            version.stale = True
    field = "reference" if kind == "scene" else "holder"
    assert save_profile(client, row, {field: ref}, kind).status_code == 422
    # wrong_type deliberately alters only the fixture identity, never the profile.
    with client.app.state.session_factory() as db:
        assert getattr(db.get(VisualAsset, UUID(row["id"])), kind + "_profile") is None


@pytest.mark.parametrize(
    "kind,profile",
    [
        ("scene", {"layout": "x" * 4001}),
        ("scene", {"time_of_day": "x" * 161}),
        ("prop", {"story_state": "x" * 2001}),
        ("prop", {"freeform": "no"}),
    ],
)
def test_profile_bounds(client, episode, kind, profile):
    row = create(client, episode["project_id"], kind.upper())
    assert save_profile(client, row, profile, kind).status_code == 422


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_scene_prop_permissions(client, role):
    register(client)
    project = create_project(client)
    rows = [create(client, project["id"], kind) for kind in ("SCENE", "PROP")]
    member = register(client, f"scene-{role}@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    for row, kind in zip(rows, ("scene", "prop"), strict=True):
        assert (
            save_profile(client, row, {}, kind).status_code
            == {"editor": 200, "viewer": 403, "outsider": 404}[role]
        )


def test_scene_prop_type_and_project_gates(client):
    register(client)
    project = create_project(client)
    row = create(client, project["id"], "SCENE")
    assert save_profile(client, row, {}, "prop").status_code == 422
    client.patch(f"/api/v1/projects/{project['id']}", json={"archived": True})
    assert save_profile(client, row, {}).status_code == 409
    client.request(
        "DELETE", f"/api/v1/projects/{project['id']}", json={"confirm_name": project["name"]}
    )
    assert save_profile(client, row, {}).status_code == 404


def test_scene_prop_migration_preservation(client, episode, migrated):
    rows = [create(client, episode["project_id"], kind) for kind in ("SCENE", "PROP")]
    command.downgrade(migrated, "0014")
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert client.get(path(episode["project_id"])).json() == rows
    assert save_profile(client, rows[0], {"lighting": "日光"}).status_code == 200
    with pytest.raises(RuntimeError, match="Scene/prop profiles exist"):
        command.downgrade(migrated, "0014")
    command.upgrade(migrated, "head")
    config = Config(migrated.config_file_name, output_buffer=StringIO())
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0015:0014", sql=True)


def test_scene_profile_concurrent_cas(client, episode, settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL locking verified in dedicated CI database")
    row = create(client, episode["project_id"], "SCENE")
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(save_profile, client, row, {"layout": "A"}),
            pool.submit(rename, client, row),
        ]
        assert sorted(call.result().status_code for call in calls) == [200, 409]
