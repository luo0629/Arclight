"""DAG-03: exact typed closure, live text/selection integration and late outputs."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import StringIO
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from test_asset_selection import approved, choose, current
from test_asset_versions import private_objects  # noqa: F401
from test_assets import create
from test_collaboration import create_project, register
from test_dependency_manifests import capture, derived, path, source_input
from test_dependency_validation import frozen_manifest, registry_version

from arclight.application.dependencies import attach_output
from arclight.application.invalidation import impacts, invalidate
from arclight.persistence.models import AssetVersion, DependencyInvalidation, VisualAsset
from arclight.providers.text import MockTextProvider


def produce(db, pid, kind, source, edge="CONTENT_DEPENDENCY", selectors=None):
    asset, version = registry_version(db, pid, kind=kind, manifest_id=uuid4())
    manifest = frozen_manifest(
        db, pid, asset, version, [source], kind=edge, patch={"selectors": selectors or []}
    )
    attach_output(db, pid, manifest.id, version)
    return asset, version


def change(source, kind="CONTENT", selectors=None):
    return {
        "source_asset_version_id": str(source.id),
        "expected_revision": source.revision,
        "change_kind": kind,
        "selectors": selectors or [],
    }


def apply(db, pid, source, kind="CONTENT", selectors=None, key=None):
    return invalidate(
        db, pid, "service-owner", key or uuid4(), [change(source, kind, selectors)], "测试变更"
    )


def test_dialogue_only_affects_related_audio_subtitle_and_their_closure(client, episode):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        script = registry_version(db, pid, kind="SCRIPT")
        audio = produce(db, pid, "AUDIO", script, selectors=["dialogue:a"])
        subtitle = produce(db, pid, "SUBTITLE", script, selectors=["dialogue:a"])
        other_audio = produce(db, pid, "AUDIO", script, selectors=["dialogue:b"])
        board = produce(db, pid, "STORYBOARD", script)
        image = produce(db, pid, "SHOT_IMAGE", board)
        preview = produce(db, pid, "SHOT_PREVIEW", image)
        timeline = produce(db, pid, "TIMELINE", audio, "RENDER_DEPENDENCY")
        render = produce(db, pid, "RENDER", timeline, "RENDER_DEPENDENCY")
        key = uuid4()
        event, created = apply(db, pid, script[1], "DIALOGUE", ["dialogue:a"], key)
        assert created
        assert {x["asset_version_id"] for x in event.snapshot_json["affected"]} == {
            str(pair[1].id) for pair in (audio, subtitle, timeline, render)
        }
        for pair in (audio, subtitle, timeline, render):
            assert pair[1].stale and pair[1].status == "STALE"
        for pair in (script, other_audio, board, image, preview):
            assert not pair[1].stale and pair[1].status == "APPROVED"
        revisions = [pair[1].revision for pair in (audio, subtitle, timeline, render)]
        assert apply(db, pid, script[1], "DIALOGUE", ["dialogue:a"], key)[0].id == event.id
        assert [pair[1].revision for pair in (audio, subtitle, timeline, render)] == revisions


@pytest.mark.parametrize("kind", ["CONTENT", "LIFECYCLE"])
def test_content_propagates_storyboard_image_preview_timeline_render(client, episode, kind):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = registry_version(db, pid, kind="SCRIPT")
        board = produce(db, pid, "STORYBOARD", source)
        image = produce(db, pid, "SHOT_IMAGE", board)
        preview = produce(db, pid, "SHOT_PREVIEW", image)
        timeline = produce(db, pid, "TIMELINE", preview, "RENDER_DEPENDENCY")
        render = produce(db, pid, "RENDER", timeline, "RENDER_DEPENDENCY")
        event, _ = apply(db, pid, source[1], kind)
        assert len(event.snapshot_json["affected"]) == 5
        assert all(pair[1].stale for pair in (board, image, preview, timeline, render))
        assert not source[1].stale


def test_sequence_only_affects_timeline_and_render(client, episode):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        board = registry_version(db, pid, kind="STORYBOARD")
        image = produce(db, pid, "SHOT_IMAGE", board)
        preview = produce(db, pid, "SHOT_PREVIEW", image)
        timeline = produce(db, pid, "TIMELINE", board, "SEQUENCE_DEPENDENCY")
        render = produce(db, pid, "RENDER", timeline, "RENDER_DEPENDENCY")
        event, _ = apply(db, pid, board[1], "SEQUENCE")
        assert {x["asset_version_id"] for x in event.snapshot_json["affected"]} == {
            str(timeline[1].id),
            str(render[1].id),
        }
        assert not image[1].stale and not preview[1].stale


@pytest.mark.parametrize(
    "change_kind,edge,expected",
    [
        ("STYLE", "STYLE_DEPENDENCY", True),
        ("STYLE", "REFERENCE_DEPENDENCY", False),
        ("REFERENCE", "REFERENCE_DEPENDENCY", True),
        ("REFERENCE", "STYLE_DEPENDENCY", False),
        ("RENDER", "RENDER_DEPENDENCY", True),
    ],
)
def test_edge_types_and_selector_matching(client, episode, change_kind, edge, expected):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = registry_version(db, pid, kind="SCENE")
        output = produce(
            db,
            pid,
            "TIMELINE" if edge == "RENDER_DEPENDENCY" else "SHOT_IMAGE",
            source,
            edge,
            ["field:one"],
        )
        assert impacts(db, pid, [change(source[1], change_kind, ["field:two"])]) == []
        apply(db, pid, source[1], change_kind, ["field"])
        assert output[1].stale is expected


def test_late_output_is_stale_with_original_manifest_and_edges(client, episode):
    pid = UUID(episode["project_id"])
    source = create(client, str(pid))
    reference = approved(client, source)
    target = create(client, str(pid), "SCENE")
    original = capture(client, target, [source_input(reference)]).json()
    with client.app.state.session_factory.begin() as db:
        before = db.get(AssetVersion, UUID(reference["id"]))
        apply(db, pid, before)
        output = AssetVersion(
            asset_id=UUID(target["id"]),
            version_no=1,
            checksum="b" * 64,
            object_key="test-only",
            created_by="service-owner",
            status="READY_FOR_REVIEW",
            metadata_json={"dependency_manifest_id": original["id"]},
        )
        db.add(output)
        db.flush()
        edges = attach_output(db, pid, UUID(original["id"]), output)
        assert output.stale and output.status == "STALE"
        assert edges[0].source_asset_version_id == before.id
        assert output.metadata_json["dependency_manifest_id"] == original["id"]
        assert len(attach_output(db, pid, UUID(original["id"]), output)) == 1
    assert client.get(path(str(pid)) + "/dependency-manifests/" + original["id"]).json() == original
    # An explicit new task may use an approved historical source. Its baseline
    # includes the event it knowingly captured, rather than pretending it was late.
    fresh = capture(client, target, [source_input(reference)])
    assert fresh.status_code == 201
    assert fresh.json()["snapshot"]["invalidation_seq"] == 1
    with client.app.state.session_factory.begin() as db:
        explicit = AssetVersion(
            asset_id=UUID(target["id"]),
            version_no=2,
            checksum="c" * 64,
            object_key="test-only",
            created_by="service-owner",
            status="READY_FOR_REVIEW",
            metadata_json={"dependency_manifest_id": fresh.json()["id"]},
        )
        db.add(explicit)
        db.flush()
        attach_output(db, pid, UUID(fresh.json()["id"]), explicit)
        assert not explicit.stale


def test_selection_invalidates_real_image_but_new_draft_does_not(client, episode):
    source, reference, target, manifest, output = derived(client, episode["project_id"])
    assert choose(client, current(client, source), reference).status_code == 200
    replacement = approved(client, current(client, source))
    url = f"/api/v1/projects/{episode['project_id']}/assets/{target['id']}/versions/{output['id']}"
    assert not client.get(url).json()["stale"]
    assert choose(client, current(client, source), replacement).status_code == 200
    assert client.get(url).json()["stale"]
    assert (
        client.get(path(episode["project_id"]) + "/dependency-manifests/" + manifest["id"]).json()
        == manifest
    )


def test_real_text_approval_uses_line_identity_and_preserves_old_json(client, episode):
    prefix = f"/api/v1/episodes/{episode['id']}"
    content = MockTextProvider().script("对白变更", {}).model_dump(mode="json")
    content["scenes"][0]["dialogue"] = [
        {
            "line_id": str(uuid4()),
            "speaker": content["characters"][0],
            "text": "原来的台词",
            "line_type": "dialogue",
            "emotion": "",
        }
    ]
    original = deepcopy(content)
    first = client.post(
        prefix + "/scripts", json={"expected_version_no": 0, "content": content}
    ).json()
    assert (
        client.post(
            prefix + f"/scripts/{first['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    pid = UUID(episode["project_id"])
    line = content["scenes"][0]["dialogue"][0]["line_id"]
    with client.app.state.session_factory.begin() as db:
        source = db.get(AssetVersion, UUID(first["id"]))
        asset = db.get(VisualAsset, source.asset_id)
        audio = produce(db, pid, "AUDIO", (asset, source), selectors=["dialogue:" + line])
        image = produce(db, pid, "SHOT_IMAGE", (asset, source))
        audio_id, image_id = audio[1].id, image[1].id
    content["scenes"][0]["dialogue"][0]["text"] = "修改后的台词"
    second = client.post(
        prefix + "/scripts",
        json={"expected_version_no": 1, "base_version_id": first["id"], "content": content},
    ).json()
    with client.app.state.session_factory() as db:
        assert not db.get(AssetVersion, audio_id).stale
    assert (
        client.post(
            prefix + f"/scripts/{second['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, audio_id).stale
        assert not db.get(AssetVersion, image_id).stale
    assert (
        client.get(prefix + "/scripts/" + first["id"] + "/comparison").json()["target"][
            "content_json"
        ]
        == original
    )


@pytest.mark.parametrize("status", ["LOCKED", "ARCHIVED", "REJECTED"])
def test_propagation_preserves_lifecycle_and_never_clears_stale(client, episode, status):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = registry_version(db, pid, kind="SCRIPT")
        output = produce(db, pid, "AUDIO", source)
        output[1].status = status
        db.flush()
        apply(db, pid, source[1])
        assert output[1].status == status and output[1].stale
        revision = output[1].revision
        apply(db, pid, source[1])
        assert output[1].revision == revision


def test_invalidation_api_cas_replay_and_cross_project(client, episode):
    source = create(client, episode["project_id"])
    reference = approved(client, source)
    url = path(episode["project_id"]) + "/dependencies/invalidate"
    body = {
        "creation_key": str(uuid4()),
        "source_asset_version_id": reference["id"],
        "expected_revision": reference["revision"],
        "reason": "撤回上游",
    }
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    assert client.post(url, json=body).json() == response.json()
    assert client.post(url, json={**body, "reason": "不同"}).status_code == 409
    assert (
        client.post(
            url, json={**body, "creation_key": str(uuid4()), "expected_revision": 0}
        ).status_code
        == 409
    )
    assert (
        client.post(
            url,
            json={**body, "creation_key": str(uuid4()), "source_asset_version_id": str(uuid4())},
        ).status_code
        == 404
    )
    assert (
        client.post(
            url, json={**body, "creation_key": str(uuid4()), "change_kind": "DIALOGUE"}
        ).status_code
        == 422
    )
    assert client.post(url, json={**body, "selectors": ["same", "same"]}).status_code == 422


def test_invalidation_history_is_immutable_and_downgrade_guarded(client, episode, migrated):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = registry_version(db, pid)
        apply(db, pid, source[1])
    with (
        pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"),
        client.app.state.engine.begin() as db,
    ):
        db.execute(DependencyInvalidation.__table__.delete())
    with pytest.raises(RuntimeError, match="invalidations exist"):
        command.downgrade(migrated, "0020")
    # Later migrations may already have been removed before 0021 refuses data loss.
    command.upgrade(migrated, "head")
    command.check(migrated)
    output = StringIO()
    config = Config(migrated.config_file_name, output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0020:0021", sql=True)
    assert "BEFORE TRUNCATE ON dependency_invalidations" in output.getvalue()
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0021:0020", sql=True)


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_only_owner_can_invalidate_and_no_foreign_data_leaks(client, role):
    owner = register(client)
    project = create_project(client)
    asset = create(client, project["id"])
    source = approved(client, asset)
    member = register(client, "dag-member@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    if role != "outsider":
        assert (
            client.post(
                path(project["id"]) + "/members",
                json={"email": member["user"]["email"], "role": role},
            ).status_code
            == 201
        )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    result = client.post(
        path(project["id"]) + "/dependencies/invalidate",
        json={
            "creation_key": str(uuid4()),
            "source_asset_version_id": source["id"],
            "expected_revision": source["revision"],
            "reason": "权限测试",
        },
    )
    assert result.status_code == (404 if role == "outsider" else 403)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(DependencyInvalidation)) == 0


def test_postgres_invalidation_racing_output_registration_is_always_stale(
    client, episode, settings
):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("requires isolated PostgreSQL")
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = registry_version(db, pid)
        target, output = registry_version(db, pid, kind="SCENE", manifest_id=uuid4())
        manifest = frozen_manifest(db, pid, target, output, [source])
        source_id, output_id, manifest_id = source[1].id, output.id, manifest.id
    barrier = Barrier(2)

    def perform(invalidation):
        with client.app.state.session_factory.begin() as db:
            barrier.wait(timeout=10)
            if invalidation:
                apply(db, pid, db.get(AssetVersion, source_id))
            else:
                attach_output(db, pid, manifest_id, db.get(AssetVersion, output_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(perform, value) for value in (True, False)]
        for future in futures:
            future.result(timeout=30)
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, output_id).stale
        assert db.scalar(select(func.count()).select_from(DependencyInvalidation)) == 1
