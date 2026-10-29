"""DAG-01: live exact dependency writes, immutable snapshots and bounded batch reads."""

import hashlib
import json
from copy import deepcopy
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from test_asset_selection import approved, current
from test_asset_versions import draft, private_objects, save, versions  # noqa: F401
from test_assets import create, rename
from test_collaboration import create_project, register
from test_immutable_history import changed

from arclight.core.errors import DomainError
from arclight.persistence.base import Base
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    AuditEvent,
    DependencyManifest,
    DependencyOutput,
    ProjectMember,
)


def path(project_id):
    return f"/api/v1/projects/{project_id}"


def capture(client, target, inputs, **extra):
    return client.post(
        path(target["project_id"]) + "/dependency-manifests",
        json={
            "creation_key": str(uuid4()),
            "target_asset_id": target["id"],
            "inputs": inputs,
            **extra,
        },
    )


def source_input(version, kind="REFERENCE_DEPENDENCY", **extra):
    return {"source_asset_version_id": version["id"], "dependency_type": kind, **extra}


def derived(client, project_id):
    source = create(client, project_id, "CHARACTER")
    reference = approved(client, source)
    target = create(client, project_id, "SCENE")
    response = capture(client, target, [source_input(reference)])
    assert response.status_code == 201, response.text
    manifest = response.json()
    output = save(client, target, dependency_manifest_id=manifest["id"])
    return source, reference, target, manifest, output


def test_capture_and_derived_import_atomically_write_exact_edges_and_batch_query(client, episode):
    source, reference, target, manifest, output = derived(client, episode["project_id"])
    base = path(episode["project_id"])
    snapshot = deepcopy(manifest)
    assert output["dependency_manifest_id"] == manifest["id"]
    assert manifest["snapshot"]["inputs"][0]["checksum"] == reference["checksum"]
    assert manifest["snapshot"]["inputs"][0]["version_no"] == reference["version_no"]
    result = client.get(base + "/dependencies", params={"source_version_id": reference["id"]})
    assert result.status_code == 200, result.text
    rows = result.json()
    assert len(rows) == 1
    assert rows[0]["source_asset_version_id"] == reference["id"]
    assert rows[0]["target_asset_version_id"] == output["id"]
    assert rows[0]["dependency_manifest_id"] == manifest["id"]
    assert (
        client.get(base + "/dependencies", params={"target_version_id": output["id"]}).json()
        == rows
    )
    for ids, direction in (([reference["id"]], "outgoing"), ([output["id"]], "incoming")):
        assert (
            client.post(
                base + "/dependencies/query", json={"version_ids": ids, "direction": direction}
            ).json()
            == rows
        )
    assert (
        client.post(
            base + "/dependencies/query", json={"version_ids": [reference["id"]], "offset": 1}
        ).json()
        == []
    )
    assert rename(client, current(client, source), "renamed").status_code == 200
    approved(client, current(client, source))
    assert client.get(base + "/dependency-manifests/" + manifest["id"]).json() == snapshot
    assert client.get(base + "/dependencies").json() == rows
    assert current(client, target)["current_version_id"] is None


def test_manifest_and_output_replay_are_exact_and_audited_once(client, episode):
    source = create(client, episode["project_id"])
    reference = approved(client, source)
    target = create(client, episode["project_id"], "SCENE")
    key = str(uuid4())
    values = {
        "creation_key": key,
        "context": {"model": "declared-input", "parameters": {"seed": 7}},
    }
    first = capture(client, target, [source_input(reference)], **values).json()
    assert capture(client, target, [source_input(reference)], **values).json() == first
    assert capture(client, target, [source_input(reference)], creation_key=key).status_code == 409
    payload = draft(target, dependency_manifest_id=first["id"])
    one = client.post(versions(target), json=payload)
    assert one.status_code == 201, one.text
    assert client.post(versions(target), json=payload).json() == one.json()
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(DependencyManifest)) == 1
        assert db.scalar(select(func.count()).select_from(DependencyOutput)) == 1
        assert db.scalar(select(func.count()).select_from(AssetDependency)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "dependency.manifest_captured")
            )
            == 1
        )


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_manifest_permissions_and_project_isolation(client, role):
    register(client)
    project = create_project(client)
    _, reference, target, manifest, _ = derived(client, project["id"])
    member = register(client, f"dag-{role}@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    expected = 404 if role == "outsider" else 200
    base = path(project["id"])
    assert client.get(base + "/dependency-manifests/" + manifest["id"]).status_code == expected
    assert client.get(base + "/dependencies").status_code == expected
    assert (
        client.post(
            base + "/dependencies/query", json={"version_ids": [reference["id"]]}
        ).status_code
        == expected
    )
    assert (
        capture(client, target, [source_input(reference)]).status_code
        == {"editor": 201, "viewer": 403, "outsider": 404}[role]
    )
    foreign = create_project(client)
    assert (
        client.get(path(foreign["id"]) + "/dependency-manifests/" + manifest["id"]).status_code
        == 404
    )
    assert (
        client.post(
            path(foreign["id"]) + "/dependencies/query", json={"version_ids": [reference["id"]]}
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "bad", ["unknown_type", "duplicate", "selectors", "foreign", "missing", "draft"]
)
def test_invalid_manifest_has_no_partial_history(client, episode, bad):
    row = create(client, episode["project_id"])
    reference = approved(client, row)
    target = create(client, episode["project_id"])
    items = [source_input(reference)]
    if bad == "unknown_type":
        items[0]["dependency_type"] = "not-a-business-edge"
    elif bad == "duplicate":
        items *= 2
    elif bad == "selectors":
        items[0]["selectors"] = ["a", "a"]
    elif bad == "foreign":
        other = create_project(client)
        items[0]["source_asset_version_id"] = approved(client, create(client, other["id"]))["id"]
    elif bad == "missing":
        items[0]["source_asset_version_id"] = str(uuid4())
    else:
        items[0]["source_asset_version_id"] = save(client, current(client, row))["id"]
    assert capture(client, target, items).status_code in {404, 409, 422}
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(DependencyManifest)) == 0
        assert db.scalar(select(func.count()).select_from(AssetDependency)) == 0


@pytest.mark.parametrize(
    "name", ["dependency_manifests", "dependency_outputs", "asset_dependencies"]
)
def test_manifest_receipt_and_new_edge_columns_are_database_immutable(client, episode, name):
    derived(client, episode["project_id"])
    engine = client.app.state.engine
    table = Base.metadata.tables[name]
    with engine.connect() as db:
        original = dict(db.execute(select(table)).mappings().one())
    for column in table.columns:
        patch = {"updated_at": original["updated_at"]}
        patch[column.name] = changed(original[column.name], column)
        with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.execute(table.update().values(**patch))
    with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
        db.execute(table.delete())
    if engine.dialect.name == "postgresql":
        with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.exec_driver_sql(f'TRUNCATE TABLE "{name}" CASCADE')
    else:
        with pytest.raises(IntegrityError, match="replacement"), engine.begin() as db:
            db.execute(table.insert().prefix_with("OR REPLACE").values(**original))
    with engine.connect() as db:
        assert dict(db.execute(select(table)).mappings().one()) == original


@pytest.mark.legacy_schema
def test_manifest_migration_nonempty_guard_and_offline_guard(client, episode, migrated):
    # Seed 0020-era image data before the independent 0022 file receipts exist.
    command.upgrade(migrated, "0021")
    derived(client, episode["project_id"])
    # Exercise 0020's guard from its own revision: PostgreSQL rolls the entire
    # downgrade transaction back, while SQLite commits earlier DDL separately.
    command.downgrade(migrated, "0020")
    with pytest.raises(RuntimeError, match="Dependency manifests exist"):
        command.downgrade(migrated, "0019")
    with client.app.state.engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0020"
    config = Config(migrated.config_file_name, output_buffer=StringIO())
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0020:0019", sql=True)
    # 0020's preservation guard leaves its schema intact; restore later migrations
    # before comparing the full current ORM metadata with the live schema.
    command.upgrade(migrated, "head")
    command.check(migrated)


@pytest.mark.parametrize(
    "body",
    [
        {"version_ids": []},
        {"version_ids": [str(uuid4())], "limit": 1001},
        {"version_ids": [str(uuid4())], "direction": "both"},
    ],
)
def test_batch_query_is_bounded(client, episode, body):
    assert (
        client.post(path(episode["project_id"]) + "/dependencies/query", json=body).status_code
        == 422
    )


def test_import_without_manifest_preserves_legacy_request_fingerprint(client, episode):
    asset = create(client, episode["project_id"])
    payload = draft(asset)
    response = client.post(versions(asset), json=payload)
    assert response.status_code == 201, response.text
    legacy = {
        key: value
        for key, value in payload.items()
        if key not in {"creation_key", "expected_revision"}
    }
    legacy.setdefault("generation", None)
    expected = hashlib.sha256(
        json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with client.app.state.session_factory() as db:
        row = db.get(AssetVersion, UUID(response.json()["id"]))
        assert row.metadata_json["request_sha256"] == expected
    assert client.post(versions(asset), json=payload).json() == response.json()


def test_output_and_edges_roll_back_together_with_private_file(
    client, episode, monkeypatch, settings
):
    import arclight.api.asset_versions as api

    source = create(client, episode["project_id"])
    reference = approved(client, source)
    target = create(client, episode["project_id"], "SCENE")
    manifest = capture(client, target, [source_input(reference)]).json()
    before = set(settings.asset_storage_path.rglob("*.image"))
    attach = api.attach_output

    def fail_after_edges(*args):
        attach(*args)
        raise DomainError("TEST_ROLLBACK", "test fault after dependency flush", 409)

    monkeypatch.setattr(api, "attach_output", fail_after_edges)
    response = client.post(
        versions(target), json=draft(target, dependency_manifest_id=manifest["id"])
    )
    assert response.status_code == 409, response.text
    assert current(client, target)["revision"] == target["revision"]
    assert client.get(versions(target)).json() == []
    assert set(settings.asset_storage_path.rglob("*.image")) == before
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(DependencyOutput)) == 0
        assert db.scalar(select(func.count()).select_from(AssetDependency)) == 0
        assert db.scalar(select(func.count()).select_from(DependencyManifest)) == 1


def test_existing_output_cannot_claim_a_new_manifest(client, episode):
    from arclight.application.dependencies import attach_output

    source = create(client, episode["project_id"])
    reference = approved(client, source)
    target = create(client, episode["project_id"], "SCENE")
    output = save(client, target)
    manifest = capture(client, current(client, target), [source_input(reference)]).json()
    with client.app.state.session_factory.begin() as db:
        row = db.get(AssetVersion, UUID(output["id"]))
        with pytest.raises(DomainError, match="产物必须在创建时"):
            attach_output(db, UUID(episode["project_id"]), UUID(manifest["id"]), row)
        assert db.scalar(select(func.count()).select_from(DependencyOutput)) == 0
        assert db.scalar(select(func.count()).select_from(AssetDependency)) == 0


def test_postgres_offline_manifest_guards(migrated):
    output = StringIO()
    config = Config(migrated.config_file_name, output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0019:0020", sql=True)
    sql = output.getvalue()
    assert "dependency_manifest_id" in sql
    for name in ("dependency_manifests", "dependency_outputs", "asset_dependencies"):
        assert f"CREATE FUNCTION immutable_{name}_guard()" in sql
        assert f'BEFORE TRUNCATE ON "{name}"' in sql
