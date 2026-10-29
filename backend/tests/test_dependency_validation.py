"""DAG-02: typed exact inputs, cycle rejection and serialized graph writes."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_asset_selection import approved, current
from test_asset_versions import private_objects  # noqa: F401
from test_assets import create
from test_collaboration import create_project
from test_dependency_manifests import capture, source_input

from arclight.application.dependencies import attach_output, capture_manifest
from arclight.core.errors import DomainError
from arclight.domain.dependencies import ManifestCreate, validate_dependency_type
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    AuditEvent,
    DependencyManifest,
    DependencyOutput,
    VisualAsset,
)


def registry_version(db, project_id, *, kind="CHARACTER", status="APPROVED", manifest_id=None):
    """Seed an isolated producer's exact version, without pretending to generate media."""
    asset = VisualAsset(project_id=project_id, asset_type=kind, name="test artifact")
    db.add(asset)
    db.flush()
    version = AssetVersion(
        asset_id=asset.id,
        version_no=1,
        checksum="a" * 64,
        object_key="test-only",
        created_by="service-owner",
        status=status,
        metadata_json={"dependency_manifest_id": str(manifest_id)} if manifest_id else {},
    )
    db.add(version)
    db.flush()
    return asset, version


def frozen_manifest(
    db, project_id, target, output, sources, *, kind="REFERENCE_DEPENDENCY", patch=None
):
    """Seed the original receipt for graph-guard tests, including legacy graph shapes."""
    inputs = [
        {
            "source_asset_version_id": str(version.id),
            "dependency_type": kind,
            "selectors": [],
            "asset_id": str(asset.id),
            "asset_type": asset.asset_type,
            "asset_name": asset.name,
            "version_no": version.version_no,
            "checksum": version.checksum,
            **(patch or {}),
        }
        for asset, version in sources
    ]
    manifest = DependencyManifest(
        id=UUID(output.metadata_json["dependency_manifest_id"]),
        project_id=project_id,
        target_asset_id=target.id,
        creation_key=uuid4(),
        request_fingerprint="b" * 64,
        created_by="service-owner",
        snapshot_json={
            "schema_version": 1,
            "target": {"asset_id": str(target.id), "asset_type": target.asset_type},
            "inputs": inputs,
            "context": {},
        },
    )
    db.add(manifest)
    db.flush()
    return manifest


@pytest.mark.parametrize(
    "source,target,kind",
    [
        ("CHARACTER", "SCENE", "REFERENCE_DEPENDENCY"),
        ("SCENE", "SHOT_IMAGE", "REFERENCE_DEPENDENCY"),
        ("STYLE", "SHOT_IMAGE", "STYLE_DEPENDENCY"),
        ("VISUAL_BIBLE", "STORYBOARD", "STYLE_DEPENDENCY"),
        ("SCRIPT", "STORYBOARD", "CONTENT_DEPENDENCY"),
        ("SCRIPT", "AUDIO", "CONTENT_DEPENDENCY"),
        ("SCRIPT", "SUBTITLE", "CONTENT_DEPENDENCY"),
        ("STORYBOARD", "SHOT_IMAGE", "CONTENT_DEPENDENCY"),
        ("SHOT_IMAGE", "SHOT_PREVIEW", "CONTENT_DEPENDENCY"),
        ("STORYBOARD", "TIMELINE", "SEQUENCE_DEPENDENCY"),
        ("TIMELINE", "RENDER", "SEQUENCE_DEPENDENCY"),
        ("AUDIO", "TIMELINE", "RENDER_DEPENDENCY"),
        ("SUBTITLE", "RENDER", "RENDER_DEPENDENCY"),
        ("TIMELINE", "RENDER", "RENDER_DEPENDENCY"),
    ],
)
def test_allowed_dependency_contracts(source, target, kind):
    validate_dependency_type(source, target, kind)


@pytest.mark.parametrize(
    "source,target,kind",
    [
        ("CHARACTER", "SCENE", "SEQUENCE_DEPENDENCY"),
        ("CHARACTER", "SCENE", "RENDER_DEPENDENCY"),
        ("AUDIO", "SHOT_IMAGE", "REFERENCE_DEPENDENCY"),
        ("SCRIPT", "SHOT_IMAGE", "REFERENCE_DEPENDENCY"),
        ("SUBTITLE", "SHOT_IMAGE", "STYLE_DEPENDENCY"),
        ("UNKNOWN", "SHOT_IMAGE", "CONTENT_DEPENDENCY"),
        ("SCRIPT", "UNKNOWN", "CONTENT_DEPENDENCY"),
        ("STYLE", "SHOT_IMAGE", "NOT_A_TYPE"),
    ],
)
def test_disallowed_dependency_contracts(source, target, kind):
    with pytest.raises(DomainError) as error:
        validate_dependency_type(source, target, kind)
    assert error.value.code == "INVALID_DEPENDENCY_TYPE"


@pytest.mark.parametrize("kind", ["SEQUENCE_DEPENDENCY", "RENDER_DEPENDENCY"])
def test_known_but_incompatible_type_is_rejected_by_api(client, episode, kind):
    source = approved(client, create(client, episode["project_id"]))
    target = create(client, episode["project_id"], "SCENE")
    response = capture(client, target, [source_input(source, kind)])
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_DEPENDENCY_TYPE"


@pytest.mark.parametrize(
    "bad",
    ["DRAFT", "READY_FOR_REVIEW", "REJECTED", "STALE", "ARCHIVED", "stale_flag", "archived_asset"],
)
def test_invalid_input_lifecycle_never_creates_a_manifest(client, episode, bad):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source_asset, source = registry_version(
            db, pid, status=bad if bad.isupper() else "APPROVED"
        )
        if bad == "stale_flag":
            source.stale = True
        if bad == "archived_asset":
            source_asset.status = "ARCHIVED"
        target, _ = registry_version(db, pid)
        body = ManifestCreate(
            creation_key=uuid4(),
            target_asset_id=target.id,
            inputs=[
                {
                    "source_asset_version_id": source.id,
                    "dependency_type": "REFERENCE_DEPENDENCY",
                }
            ],
        )
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as error:
            capture_manifest(db, pid, "service-owner", body)
        assert error.value.code == "ASSET_NOT_USABLE"
        assert db.scalar(select(func.count()).select_from(DependencyManifest)) == 0


def test_same_asset_older_exact_version_is_a_legal_input(client, episode):
    asset = create(client, episode["project_id"])
    source = approved(client, asset)
    result = capture(client, current(client, asset), [source_input(source)])
    assert result.status_code == 201, result.text


@pytest.mark.parametrize(
    "bad",
    [
        "foreign_target",
        "foreign_output",
        "foreign_source",
        "missing_source",
        "checksum",
        "version_no",
        "asset_id",
        "asset_type",
        "type",
    ],
)
def test_internal_writer_checks_all_version_identities(client, episode, bad):
    pid = UUID(episode["project_id"])
    foreign = UUID(create_project(client)["id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        source_asset, source = registry_version(db, foreign if bad == "foreign_source" else pid)
        target, output = registry_version(
            db, foreign if bad == "foreign_output" else pid, manifest_id=uuid4()
        )
        patches = {
            "missing_source": {"source_asset_version_id": str(uuid4())},
            "checksum": {"checksum": "c" * 64},
            "version_no": {"version_no": 99},
            "asset_id": {"asset_id": str(uuid4())},
            "asset_type": {"asset_type": "PROP"},
            "type": {"dependency_type": "SEQUENCE_DEPENDENCY"},
        }
        manifest = frozen_manifest(
            db,
            foreign if bad == "foreign_target" else pid,
            target,
            output,
            [(source_asset, source)],
            patch=patches.get(bad),
        )
        mid, vid = manifest.id, output.id
    with factory.begin() as db:
        with pytest.raises(DomainError):
            attach_output(db, pid, mid, db.get(AssetVersion, vid))
        assert db.scalar(select(func.count()).select_from(DependencyOutput)) == 0
        assert db.scalar(select(func.count()).select_from(AssetDependency)) == 0


@pytest.mark.parametrize("shape", ["self", "multi_hop", "legacy_cycle", "mixed_batch"])
def test_self_and_reachable_cycles_are_rejected_without_partial_rows(client, episode, shape):
    pid = UUID(episode["project_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        target, output = registry_version(db, pid, manifest_id=uuid4())
        middle_asset, middle = registry_version(db, pid)
        source_asset, source = registry_version(db, pid)
        inputs = [(target, output)] if shape == "self" else [(source_asset, source)]
        if shape == "mixed_batch":
            inputs.insert(0, registry_version(db, pid))
        manifest = frozen_manifest(db, pid, target, output, inputs)
        edges = [] if shape == "self" else [(output.id, middle.id), (middle.id, source.id)]
        if shape == "legacy_cycle":
            edges.append((source.id, middle.id))
        db.add_all(
            [
                AssetDependency(
                    source_asset_version_id=a,
                    target_asset_version_id=b,
                    dependency_type="REFERENCE_DEPENDENCY",
                )
                for a, b in edges
            ]
        )
        mid, vid, before = manifest.id, output.id, len(edges)
    with factory.begin() as db:
        with pytest.raises(DomainError) as error:
            attach_output(db, pid, mid, db.get(AssetVersion, vid))
        assert error.value.code == "DEPENDENCY_CYCLE"
        assert db.scalar(select(func.count()).select_from(DependencyOutput)) == 0
        assert db.scalar(select(func.count()).select_from(AssetDependency)) == before


def test_lock_refreshes_a_preloaded_source_after_concurrent_change(client, episode):
    pid = UUID(episode["project_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        _, source = registry_version(db, pid)
        target, _ = registry_version(db, pid)
        vid = source.id
        body = ManifestCreate(
            creation_key=uuid4(),
            target_asset_id=target.id,
            inputs=[
                {
                    "source_asset_version_id": vid,
                    "dependency_type": "REFERENCE_DEPENDENCY",
                }
            ],
        )
    with factory.begin() as db:
        preloaded = db.get(AssetVersion, vid)
        assert not preloaded.stale
        with factory.begin() as other:
            other.get(AssetVersion, vid).stale = True
        with pytest.raises(DomainError) as error:
            capture_manifest(db, pid, "service-owner", body)
        assert error.value.code == "ASSET_NOT_USABLE"


def test_postgres_opposing_writes_are_serialized_before_cycle_validation(client, episode, settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL graph write serialization runs in isolated CI database")
    pid = UUID(episode["project_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        a, av = registry_version(db, pid, manifest_id=uuid4())
        b, bv = registry_version(db, pid, manifest_id=uuid4())
        ma = frozen_manifest(db, pid, a, av, [(b, bv)])
        mb = frozen_manifest(db, pid, b, bv, [(a, av)])
        pairs = [(ma.id, av.id), (mb.id, bv.id)]
    barrier = Barrier(2)

    def write(pair):
        try:
            with factory.begin() as db:
                output = db.get(AssetVersion, pair[1])
                barrier.wait(timeout=10)
                attach_output(db, pid, pair[0], output)
            return "ok"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, pairs)) == ["DEPENDENCY_CYCLE", "ok"]
    with factory.begin() as db:
        assert db.scalar(select(func.count()).select_from(AssetDependency)) == 1
        assert db.scalar(select(func.count()).select_from(DependencyOutput)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "dependency.output_attached")
            )
            == 1
        )
        receipt = db.scalar(select(DependencyOutput))
        assert (
            len(
                attach_output(
                    db,
                    pid,
                    receipt.dependency_manifest_id,
                    db.get(AssetVersion, receipt.asset_version_id),
                )
            )
            == 1
        )


def test_postgres_concurrent_manifest_replay_has_one_receipt(client, episode, settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL graph write serialization runs in isolated CI database")
    pid = UUID(episode["project_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        _, source = registry_version(db, pid)
        target, _ = registry_version(db, pid)
        body = ManifestCreate(
            creation_key=uuid4(),
            target_asset_id=target.id,
            inputs=[
                {
                    "source_asset_version_id": source.id,
                    "dependency_type": "REFERENCE_DEPENDENCY",
                }
            ],
        )
    barrier = Barrier(2)

    def write(_index):
        barrier.wait(timeout=10)
        with factory.begin() as db:
            row, created = capture_manifest(db, pid, "service-owner", body)
            return row.id, created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(item[1] for item in results) == [False, True]
    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "dependency.manifest_captured")
            )
            == 1
        )
