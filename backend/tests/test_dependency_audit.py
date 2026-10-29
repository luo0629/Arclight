"""DAG-07: transactional exact audit, replay, batch atomicity and PG races."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_asset_selection import approved, choose, current
from test_asset_versions import private_objects, save  # noqa: F401
from test_assets import create
from test_collaboration import create_project, register
from test_dependency_invalidation import change, produce
from test_dependency_manifests import capture, source_input
from test_dependency_validation import frozen_manifest, registry_version

from arclight.application import invalidation
from arclight.application.dependencies import attach_output, capture_manifest
from arclight.application.invalidation import invalidate
from arclight.domain.dependencies import ManifestCreate
from arclight.main import create_app
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    AuditEvent,
    DependencyInvalidation,
    VisualAsset,
)
from arclight.providers.text import MockTextProvider


def path(pid):
    return f"/api/v1/projects/{pid}"


def audits(client, pid, action):
    response = client.get(path(pid) + "/audit-events?limit=100")
    assert response.status_code == 200, response.text
    return [row for row in response.json() if row["action"] == action]


def batch(client, pid, changes, **extra):
    return client.post(
        path(pid) + "/dependencies/invalidate-batch",
        json={
            "creation_key": str(uuid4()),
            "reason": "批量上游变更",
            "changes": changes,
            **extra,
        },
    )


def test_internal_manifest_and_output_audit_once_with_late_input_reason(client, episode):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = registry_version(db, pid)
        target, output = registry_version(db, pid, kind="SCENE", manifest_id=uuid4())
        # This fixture's producer is outside HTTP, as are text and worker outputs.
        body = ManifestCreate(
            creation_key=uuid4(),
            target_asset_id=target.id,
            inputs=[
                {
                    "source_asset_version_id": source[1].id,
                    "dependency_type": "REFERENCE_DEPENDENCY",
                }
            ],
        )
        manifest, created = capture_manifest(db, pid, "capturing-actor", body)
        assert created
        assert capture_manifest(db, pid, "retry-actor", body)[0].id == manifest.id
        # Create the output with the actual manifest instead of mutating history.
        fresh = AssetVersion(
            asset_id=target.id,
            version_no=2,
            object_key="test-only",
            checksum="f" * 64,
            created_by="producing-actor",
            status="READY_FOR_REVIEW",
            metadata_json={"dependency_manifest_id": str(manifest.id)},
        )
        db.add(fresh)
        db.flush()
        invalidate(db, pid, "owner-actor", uuid4(), [change(source[1])], "输入更换")
        attached = attach_output(db, pid, manifest.id, fresh)
        assert attach_output(db, pid, manifest.id, fresh)[0].id == attached[0].id
        mid, vid, sid = str(manifest.id), str(fresh.id), str(source[1].id)
    captured = audits(client, pid, "dependency.manifest_captured")
    assert len(captured) == 1 and captured[0]["actor_id"] == "capturing-actor"
    assert captured[0]["resource_id"] == mid
    assert captured[0]["details"]["input_version_ids"] == [sid]
    outputs = audits(client, pid, "dependency.output_attached")
    assert len(outputs) == 1 and outputs[0]["actor_id"] == "producing-actor"
    assert outputs[0]["resource_id"] == vid
    details = outputs[0]["details"]
    assert details["dependency_manifest_id"] == mid
    assert details["expired_input_version_ids"] == [sid]
    assert details["status"] == "STALE" and details["stale"] and details["revision"] == 1
    assert details["edges"][0]["source_asset_version_id"] == sid
    assert "object_key" not in str(details)


def test_batch_overlap_is_one_event_one_transition_and_exact_replay(client, episode):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        a = registry_version(db, pid, kind="SCENE")
        b = registry_version(db, pid, kind="CHARACTER")
        target, output = registry_version(db, pid, kind="SHOT_IMAGE", manifest_id=uuid4())
        manifest = frozen_manifest(db, pid, target, output, [a, b])
        attach_output(db, pid, manifest.id, output)
        timeline = produce(db, pid, "TIMELINE", (target, output), "RENDER_DEPENDENCY")
        changes = [change(a[1]), change(b[1])]
        ids = [output.id, timeline[1].id]
    key = str(uuid4())
    response = batch(client, pid, changes, creation_key=key)
    assert response.status_code == 201, response.text
    event = response.json()
    assert event["event_no"] == 1
    assert (
        client.post(
            path(pid) + "/dependencies/invalidate-batch",
            json={"creation_key": key, "reason": "批量上游变更", "changes": changes},
        ).json()
        == event
    )
    assert batch(client, pid, changes, creation_key=key, reason="不同请求").status_code == 409
    rows = audits(client, pid, "dependency.invalidated")
    assert len(rows) == 1 and rows[0]["resource_id"] == event["id"]
    assert rows[0]["details"] == {"creation_key": key, "event_no": 1, **event["snapshot"]}
    assert {item["asset_version_id"] for item in event["snapshot"]["affected"]} == {
        str(v) for v in ids
    }
    for item in event["snapshot"]["affected"]:
        assert item["revision"] == 0 and item["revision_after"] == 1
        assert not item["stale"] and item["stale_after"] and item["changed"]
        assert item["status"] == "APPROVED" and item["status_after"] == "STALE"
    with client.app.state.session_factory() as db:
        assert all(db.get(AssetVersion, vid).revision == 1 for vid in ids)
        assert db.scalar(select(func.count()).select_from(DependencyInvalidation)) == 1
        assert db.scalar(select(func.count()).select_from(AssetDependency)) == 3


@pytest.mark.parametrize("failure", ["revision", "foreign", "scope"])
def test_batch_validates_every_source_before_any_stale_or_audit_write(client, episode, failure):
    pid = UUID(episode["project_id"])
    foreign = create_project(client)
    with client.app.state.session_factory.begin() as db:
        source = registry_version(db, pid)
        second = registry_version(db, UUID(foreign["id"]) if failure == "foreign" else pid)
        output = produce(db, pid, "SHOT_IMAGE", source)
        changes = [change(source[1]), change(second[1])]
        if failure == "revision":
            changes[1]["expected_revision"] = 999
        if failure == "scope":
            changes[1]["change_kind"] = "DIALOGUE"
        before = db.scalar(select(func.count()).select_from(AuditEvent))
        vid = output[1].id
    response = batch(client, pid, changes)
    assert response.status_code == {"revision": 409, "foreign": 404, "scope": 422}[failure]
    with client.app.state.session_factory() as db:
        assert not db.get(AssetVersion, vid).stale
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == before
        assert db.scalar(select(func.count()).select_from(DependencyInvalidation)) == 0


def test_batch_bounds_and_duplicates_are_rejected_without_events(client, episode):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = registry_version(db, pid)
        changes = [change(source[1])]
    for items in ([], changes * 2, changes * 101):
        assert batch(client, pid, items).status_code == 422
    assert batch(client, pid, changes, reason=" ").status_code == 422
    assert audits(client, pid, "dependency.invalidated") == []


def test_audit_failure_rolls_back_entire_propagation_transaction(client, episode, monkeypatch):
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = registry_version(db, pid)
        output = produce(db, pid, "SHOT_IMAGE", source)
        changes, vid = [change(source[1])], output[1].id

    def fail(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(invalidation, "audit", fail)
    with pytest.raises(RuntimeError, match="audit write failed"):
        batch(client, pid, changes)
    with client.app.state.session_factory() as db:
        assert not db.get(AssetVersion, vid).stale
        assert db.get(AssetVersion, vid).revision == 0
        assert db.scalar(select(func.count()).select_from(DependencyInvalidation)) == 0


def test_selection_and_propagation_audit_are_linked_and_stale_cas_cannot_duplicate(client, episode):
    pid = episode["project_id"]
    source = create(client, pid)
    first = approved(client, source)
    choose(client, current(client, source), first)
    target = create(client, pid, "SCENE")
    manifest = capture(client, target, [source_input(first)]).json()
    output = save(client, target, dependency_manifest_id=manifest["id"])
    replacement = approved(client, current(client, source))
    prior = current(client, source)
    assert choose(client, prior, replacement).status_code == 200
    assert choose(client, prior, replacement).status_code == 409
    selected = audits(client, pid, "asset.version_selected")
    propagation = audits(client, pid, "dependency.invalidated")
    assert len(selected) == 2 and len(propagation) == 1
    latest = next(row for row in selected if row["details"]["version_id"] == replacement["id"])
    assert latest["details"]["previous_version_id"] == first["id"]
    assert latest["details"]["dependency_invalidation_id"] == propagation[0]["resource_id"]
    assert propagation[0]["details"]["affected"][0]["asset_version_id"] == output["id"]
    assert client.get(path(pid) + "/dependency-manifests/" + manifest["id"]).json() == manifest


def test_text_approval_selects_exact_registry_version_with_audit(client, episode):
    prefix = f"/api/v1/episodes/{episode['id']}/scripts"
    content = MockTextProvider().script("审计", {}).model_dump(mode="json")
    saved = client.post(prefix, json={"expected_version_no": 0, "content": content}).json()
    assert (
        client.post(prefix + f"/{saved['id']}/review", json={"decision": "APPROVE"}).status_code
        == 200
    )
    rows = audits(client, episode["project_id"], "asset.version_selected")
    assert len(rows) == 1 and rows[0]["details"]["version_id"] == saved["id"]
    assert rows[0]["details"]["trigger"] == "TEXT_APPROVAL"
    with client.app.state.session_factory.begin() as db:
        source = db.get(AssetVersion, UUID(saved["id"]))
        produce(
            db, UUID(episode["project_id"]), "AUDIO", (db.get(VisualAsset, source.asset_id), source)
        )
    content["summary"] = "更改已批准剧本的内容"
    next_version = client.post(prefix, json={"expected_version_no": 1, "content": content}).json()
    assert (
        client.post(
            prefix + f"/{next_version['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    next_audit = next(
        row
        for row in audits(client, episode["project_id"], "asset.version_selected")
        if row["details"]["version_id"] == next_version["id"]
    )
    event = audits(client, episode["project_id"], "dependency.invalidated")[0]
    assert next_audit["details"]["dependency_invalidation_id"] == event["resource_id"]
    assert event["details"]["changes"][0]["source_asset_version_id"] == saved["id"]


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_batch_and_audit_read_permissions_never_leak_foreign_records(client, role):
    owner = register(client)
    project = create_project(client)
    source = create(client, project["id"])
    version = approved(client, source)
    changes = [{"source_asset_version_id": version["id"], "expected_revision": version["revision"]}]
    assert batch(client, project["id"], changes).status_code == 201
    member = register(client, "audit-member@example.com")
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
    expected = 404 if role == "outsider" else 403
    assert batch(client, project["id"], changes).status_code == expected
    assert client.get(path(project["id"]) + "/audit-events").status_code == expected


@pytest.mark.parametrize("replay", [True, False])
def test_postgres_simultaneous_events_have_consistent_exact_audit(
    client, episode, settings, replay
):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("requires isolated PostgreSQL row locks")
    pid = UUID(episode["project_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        source = registry_version(db, pid)
        output = produce(db, pid, "SHOT_IMAGE", source)
        changes, vid = [change(source[1])], output[1].id
    key = uuid4()
    barrier = Barrier(2)

    def perform(index):
        barrier.wait(timeout=15)
        with factory.begin() as db:
            row, created = invalidate(
                db, pid, f"actor-{index}", key if replay else uuid4(), changes, "并发传播"
            )
            return row.id, created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(perform, range(2)))
    rows = audits(client, pid, "dependency.invalidated")
    assert len(rows) == (1 if replay else 2)
    assert sum(result[1] for result in results) == len(rows)
    assert len({result[0] for result in results}) == len(rows)
    assert sorted(row["details"]["event_no"] for row in rows) == list(range(1, len(rows) + 1))
    assert sum(row["details"]["affected"][0]["changed"] for row in rows) == 1
    assert (
        sum(
            row["details"]["affected"][0]["revision_after"]
            - row["details"]["affected"][0]["revision"]
            for row in rows
        )
        == 1
    )
    with factory() as db:
        assert db.get(AssetVersion, vid).revision == 1


def test_postgres_concurrent_selection_has_one_winner_and_one_linked_propagation(
    client, episode, settings
):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("requires isolated PostgreSQL row locks")
    pid = episode["project_id"]
    asset = create(client, pid)
    first = approved(client, asset)
    choose(client, current(client, asset), first)
    second = approved(client, current(client, asset))
    third = approved(client, current(client, asset))
    before = current(client, asset)
    with client.app.state.session_factory.begin() as db:
        source = db.get(AssetVersion, UUID(first["id"]))
        output = produce(
            db, UUID(pid), "SHOT_IMAGE", (db.get(VisualAsset, source.asset_id), source)
        )
        output_id = str(output[1].id)
    barrier = Barrier(2)

    def select_candidate(version):
        with TestClient(create_app(settings)) as connection:
            barrier.wait(timeout=15)
            return connection.post(
                path(pid) + f"/assets/{asset['id']}/versions/{version['id']}/select",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "expected_revision": before["revision"],
                    "expected_lock_version": before["lock_version"],
                },
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(select_candidate, (second, third)))
    assert sorted(row.status_code for row in results) == [200, 409]
    events = audits(client, pid, "dependency.invalidated")
    selections = audits(client, pid, "asset.version_selected")
    assert len(events) == 1 and len(selections) == 2
    assert events[0]["details"]["affected"][0]["asset_version_id"] == output_id
    assert events[0]["details"]["affected"][0]["changed"]
    winner = next(row.json()["current_version_id"] for row in results if row.status_code == 200)
    assert current(client, asset)["current_version_id"] == winner
    assert (
        next(row for row in selections if row["details"]["version_id"] == winner)["details"][
            "dependency_invalidation_id"
        ]
        == events[0]["resource_id"]
    )
