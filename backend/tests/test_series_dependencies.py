"""SERIES-05: exact cross-episode reads, confirmed impact and selective rebuild."""

from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy import func, select
from test_dependency_invalidation import produce
from test_dependency_validation import frozen_manifest, registry_version
from test_script_state_commit import capture, save, screenplay
from test_series_planning import planned_outline as planned_outline
from test_story_ledger import (
    approve_ledger,
    approved_script,
    ledger_content,
    ledger_path,
    save_ledger,
)
from test_story_outline import save as save_outline
from test_writing_context import writing_case as writing_case

from arclight.application.dependencies import attach_output
from arclight.persistence.models import (
    AssetVersion,
    DependencyInvalidation,
    DependencyManifest,
    GenerationJob,
    Script,
    Shot,
    Storyboard,
    VisualAsset,
    WorkflowRun,
)


def _episode(client, case, number):
    response = client.post(
        f"/api/v1/projects/{case['episode']['project_id']}/episodes",
        json={"episode_no": number, "title": f"第{number}集", "target_duration_ms": 30000},
    )
    assert response.status_code == 201, response.text
    return case | {"episode": response.json()}


def _draft(client, case, story_time, *, plan=None, changes=False, **revision):
    head = client.get(ledger_path(case["episode"])).json()
    context = capture(
        client,
        case,
        target_story_time=story_time,
        ledger_version_id=head["approved_version_id"],
        expected_ledger_head_revision=head["revision"],
        **({"series_plan_version_id": plan} if plan else {}),
    )
    value = screenplay(case, context)
    if not changes:
        value["scenes"][0]["state_changes"] = []
    return save(client, case, value, **revision)


def _script_url(case, script):
    return f"/api/v1/episodes/{case['episode']['id']}/scripts/{script['id']}"


def _preview(client, url, suffix="/dependency-impact"):
    response = client.get(url + suffix)
    assert response.status_code == 200, response.text
    return response.json()


def _review(client, url, **fields):
    return client.post(url + "/review", json={"decision": "APPROVE", **fields})


def _next_plan(client, case, base, change_episode):
    content = deepcopy(base["content"])
    content["series_plan"]["episode_progressions"][change_episode - 1]["ending_handoff"] += (
        "后来，收信人决定自己过河。"
    )
    response = save_outline(client, case["episode"], base, content, str(uuid4()))
    assert response.status_code == 201, response.text
    return response.json()


def _plan_url(case, plan):
    return f"/api/v1/episodes/{case['episode']['id']}/story-outlines/{plan['id']}"


def test_plan_approval_confirms_only_the_projected_episode_and_rejects_late_output(
    client, writing_case
):
    case = writing_case
    later, unrelated = _episode(client, case, 2), _episode(client, case, 3)
    source = _draft(client, later, 10)
    unaffected = _draft(client, unrelated, 10)
    new_plan = _next_plan(client, case, case["plan"], 2)
    url = _plan_url(case, new_plan)
    impact = _preview(client, url, "/approval-impact")
    assert _preview(client, url, "/approval-impact") == impact
    assert [item["asset_version_id"] for item in impact["affected"]] == [source["id"]]
    item = impact["affected"][0]
    assert item["episode_no"] == 2
    assert item["source_version_id"] == case["plan"]["id"]
    assert item["replacement_version_id"] == new_plan["id"]
    assert item["source_kind"] == "SERIES_PLAN" and "全剧规划" in item["reason"]
    error = client.post(url + "/confirm")
    assert (
        error.status_code == 409 and error.json()["error"]["code"] == "DEPENDENCY_IMPACT_REQUIRED"
    )
    # A second output of the same frozen plan enters the graph before approval.
    raced = save(client, later, source["content_json"], expected=1, base=source["id"])
    error = client.post(url + "/confirm", json={"impact_digest": impact["digest"]})
    assert error.status_code == 409 and error.json()["error"]["code"] == "DEPENDENCY_IMPACT_CHANGED"
    fresh = _preview(client, url, "/approval-impact")
    assert {item["asset_version_id"] for item in fresh["affected"]} == {source["id"], raced["id"]}
    approval = client.post(url + "/confirm", json={"impact_digest": fresh["digest"]})
    assert approval.status_code == 200, approval.text
    assert (
        client.post(url + "/confirm", json={"impact_digest": fresh["digest"]}).json()
        == approval.json()
    )

    late = save(client, later, source["content_json"], expected=2, base=raced["id"])
    assert late["status"] == "STALE"
    with client.app.state.session_factory() as db:
        assert db.get(Script, UUID(unaffected["id"])).status == "READY_FOR_REVIEW"
        assert db.get(Script, UUID(source["id"])).content_json == source["content_json"]
        assert db.get(Script, UUID(raced["id"])).status == "STALE"
        assert db.get(AssetVersion, UUID(late["id"])).stale
        events = list(
            db.scalars(select(DependencyInvalidation).order_by(DependencyInvalidation.event_no))
        )
        assert len(events) == 2  # The original v1->v2 plan approval, then one replacement.
        assert events[-1].snapshot_json["narrative"]["version_id"] == new_plan["id"]


def test_out_of_order_plan_approval_cannot_readmit_an_obsolete_branch_output(client, writing_case):
    case = writing_case
    later = _episode(client, case, 2)
    second = _next_plan(client, case, case["plan"], 2)
    third = _next_plan(client, case, second, 2)
    for plan in (third, second):
        response = client.post(_plan_url(case, plan) + "/confirm")
        assert response.status_code == 200, response.text
    stale = _draft(client, later, 10, plan=second["id"])
    assert stale["status"] == "STALE"
    fresh = _draft(client, later, 10, plan=third["id"], expected=1, base=stale["id"])
    assert fresh["status"] == "READY_FOR_REVIEW"


def test_ledger_knowledge_change_preserves_flashback_and_exact_media_history(client, writing_case):
    case = writing_case
    origin = approved_script(client, case["episode"])
    value = ledger_content(case["episode"], origin, case["characters"][0]["id"])
    value["entries"][0].update(
        kind="RELATIONSHIP",
        target_id=case["characters"][1]["id"],
        story_start=15,
        value="阿青已经取得石岚的信任",
    )
    value["entries"][0]["known_by"][0]["story_start"] = 15
    baseline = save_ledger(client, case["episode"], value)
    approve_ledger(client, case["episode"], baseline)
    later, flashback = _episode(client, case, 2), _episode(client, case, 3)
    reader = _draft(client, later, 20)
    review = _review(
        client,
        _script_url(later, reader),
        impact_digest=_preview(client, _script_url(later, reader))["digest"],
    )
    assert review.status_code == 200, review.text
    unrelated = _draft(client, flashback, 0)

    pid = UUID(case["episode"]["project_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        source = db.get(AssetVersion, UUID(reader["id"]))
        script = (db.get(VisualAsset, source.asset_id), source)
        board = produce(db, pid, "STORYBOARD", script)
        board[0].metadata_json = {"episode_id": later["episode"]["id"]}
        db.add(
            Storyboard(
                id=board[1].id,
                episode_id=UUID(later["episode"]["id"]),
                script_version_id=source.id,
                version_no=1,
                status="APPROVED",
                content_json={},
            )
        )
        db.flush()
        shot = Shot(
            storyboard_id=board[1].id,
            shot_no=1,
            status="APPROVED",
            duration_ms=1000,
            description="动态表演",
            content_json={},
        )
        db.add(shot)
        audio = produce(db, pid, "AUDIO", script)
        video = produce(db, pid, "VIDEO", board)
        timeline = produce(db, pid, "TIMELINE", video, "RENDER_DEPENDENCY")
        render = produce(db, pid, "RENDER", timeline, "RENDER_DEPENDENCY")
        # Publication is a pointer to an immutable output, not permission to rewrite it.
        render[0].current_version_id, render[1].status = render[1].id, "LOCKED"
        published = (render[0].id, render[1].id, render[1].object_key, render[1].checksum)
        ids = {str(pair[1].id) for pair in (script, board, audio, video, timeline, render)}
        late = registry_version(db, pid, kind="VIDEO", manifest_id=uuid4())
        pending = frozen_manifest(db, pid, *late, [board], kind="CONTENT_DEPENDENCY")
        late_id, manifest_id = late[1].id, pending.id
        run_count = db.scalar(select(func.count()).select_from(WorkflowRun))
        job_count = db.scalar(select(func.count()).select_from(GenerationJob))

    prefix = ledger_path(case["episode"])
    head = client.get(prefix).json()
    original = client.get(prefix + "/versions/" + head["approved_version_id"]).json()
    replacement = deepcopy(original["content"])
    replacement["entries"][0]["known_by"][0]["character_id"] = case["characters"][1]["id"]
    new = save_ledger(
        client,
        case["episode"],
        replacement,
        revision=head["revision"],
        base_version_id=original["id"],
        key="knowledge-change",
    )
    url = prefix + "/versions/" + new["version"]["id"]
    impact = _preview(client, url, "/approval-impact")
    affected = {item["asset_version_id"]: item for item in impact["affected"]}
    assert set(affected) == ids
    assert "角色视角" in affected[reader["id"]]["reason"]
    assert affected[reader["id"]]["reviews"][0]["kind"] == "TEXT_SCRIPT"
    assert (
        next(x for x in affected.values() if x["asset_type"] == "STORYBOARD")["shots"][0]["shot_no"]
        == 1
    )
    payload = {"expected_revision": new["head"]["revision"], "impact_digest": impact["digest"]}
    headers = {"Idempotency-Key": "knowledge-approval"}
    response = client.post(url + "/approve", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert client.post(url + "/approve", json=payload, headers=headers).json() == response.json()
    assert client.get(prefix + "/versions/" + original["id"]).json() == original

    with factory.begin() as db:
        assert all(db.get(AssetVersion, UUID(identity)).stale for identity in ids)
        assert db.get(Shot, shot.id).status == "STALE"
        archive = db.get(AssetVersion, published[1])
        assert (archive.status, archive.object_key, archive.checksum) == ("LOCKED", *published[2:])
        assert db.get(VisualAsset, published[0]).current_version_id == archive.id
        assert not db.get(AssetVersion, UUID(unrelated["id"])).stale
        attach_output(db, pid, manifest_id, db.get(AssetVersion, late_id))
        assert db.get(AssetVersion, late_id).stale
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == run_count
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == job_count

    rebuilt = _draft(client, later, 20, expected=1, base=reader["id"])
    new_url = _script_url(later, rebuilt)
    approved = _review(client, new_url, impact_digest=_preview(client, new_url)["digest"])
    assert approved.status_code == 200, approved.text
    assert client.get(_script_url(later, reader)).json()["content_json"] == reader["content_json"]
    with factory() as db:
        registry = db.get(AssetVersion, UUID(rebuilt["id"]))
        receipt = db.get(DependencyManifest, UUID(registry.metadata_json["dependency_manifest_id"]))
        assert (
            receipt.snapshot_json["narrative"]["context"]["ledger_version_id"]
            == new["version"]["id"]
        )
        assert registry.status == "APPROVED" and not registry.stale


def test_earlier_screenplay_and_new_fact_commit_with_the_same_confirmed_impact(
    client, writing_case
):
    case = writing_case
    later = _episode(client, case, 2)
    reader = _draft(client, later, 20)
    source = _draft(client, case, 10, changes=True)
    url = _script_url(case, source)
    impact = _preview(client, url)
    assert [item["asset_version_id"] for item in impact["affected"]] == [reader["id"]]
    assert impact["affected"][0]["source_kind"] == "STORY_LEDGER"
    refused = _review(client, url)
    assert (
        refused.status_code == 409
        and refused.json()["error"]["code"] == "DEPENDENCY_IMPACT_REQUIRED"
    )
    assert client.get(ledger_path(case["episode"])).json()["approved_version_id"] is None
    accepted = _review(client, url, impact_digest=impact["digest"])
    assert accepted.status_code == 200, accepted.text
    assert client.get(_script_url(later, reader)).json()["status"] == "STALE"
    assert client.get(url).json()["status"] == "APPROVED"
