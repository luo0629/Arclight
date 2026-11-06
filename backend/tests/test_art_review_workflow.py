"""ART-04 real manual CRUD and transactional formal approvals through FLOW-03."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_art_proposals import proposal_payload
from test_art_protection import acknowledgement, bible_payload
from test_asset_versions import private_objects  # noqa: F401

from arclight.domain.art_protection import ArtComparison
from arclight.persistence.art_proposals import ArtApprovedVersion
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import AuditEvent, VisualAsset, VisualBibleVersion


def path(project_id):
    return f"/api/v1/projects/{project_id}/art-proposals"


def save(client, episode, kind="CHARACTER", *, payload=None):
    payload = payload or (
        bible_payload(client, episode, palette="蓝灰")
        if kind == "VISUAL_BIBLE"
        else proposal_payload(kind, episode["project_id"])
    )
    body = {"creation_key": str(uuid4()), "proposal": payload}
    response = client.post(path(episode["project_id"]), json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


def submit(client, saved, *, option=0):
    version = saved["version"]
    body = {
        "version_id": version["id"],
        "expected_revision": saved["revision"],
        "option_id": version["comparison_json"]["options"][option]["option_id"],
        "comparison_sha256": version["comparison_json"]["comparison_sha256"],
    }
    return client.post(path(saved["project_id"]) + f"/{saved['id']}/submit", json=body)


def decision(saved, value="APPROVE"):
    body = {
        "version_id": saved["version"]["id"],
        "expected_revision": saved["revision"],
        "request_sha256": saved["review"]["request_sha256"],
        "decision": value,
        "comment": "人工确认完整变更" if value == "APPROVE" else "保留原设定",
    }
    if value == "APPROVE":
        body["confirmation"] = acknowledgement(
            ArtComparison.model_validate(saved["version"]["comparison_json"])
        ).model_dump(mode="json")
    return body


def decide(client, saved, value="APPROVE", *, body=None):
    return client.post(
        path(saved["project_id"]) + f"/{saved['id']}/decisions", json=body or decision(saved, value)
    )


def effects(client):
    with client.app.state.session_factory() as db:
        return {
            table.__tablename__: db.scalar(select(func.count()).select_from(table))
            for table in (
                VisualAsset,
                VisualBibleVersion,
                ArtApprovedVersion,
                HumanReviewRequest,
                HumanReviewDecision,
                HumanReviewResume,
                AuditEvent,
            )
        }


def existing_payload(client, episode, kind="CHARACTER"):
    asset = client.post(
        f"/api/v1/projects/{episode['project_id']}/assets",
        json={"asset_type": kind, "name": "原设定"},
    ).json()
    payload = proposal_payload(kind, episode["project_id"])
    baseline = client.get(path(episode["project_id"]) + f"/baselines/assets/{asset['id']}").json()
    payload["options"][0]["designs"][0]["target"] = baseline["target"]
    return payload, asset


@pytest.mark.parametrize("kind", ["CHARACTER", "SCENE", "PROP", "STYLE", "VISUAL_BIBLE"])
def test_manual_five_kinds_publish_only_after_exact_owner_approval(client, episode, kind):
    before = effects(client)
    saved, body = save(client, episode, kind)
    assert saved["version"]["version_no"] == 1 and saved["review"] is None
    assert effects(client)["visual_assets"] == before["visual_assets"]
    assert effects(client)["visual_bible_versions"] == before["visual_bible_versions"]
    replay = client.post(path(episode["project_id"]), json=body)
    assert replay.status_code == 200 and replay.json()["version"] == saved["version"]
    waiting = submit(client, saved)
    assert waiting.status_code == 200, waiting.text
    waiting = waiting.json()
    assert waiting["review"]["status"] == "WAITING_REVIEW"
    assert waiting["review"]["snapshot"]["version_id"] == saved["version"]["id"]
    assert waiting["review"]["snapshot"]["thread_id"] == "art:" + saved["version"]["id"]
    approved = decide(client, waiting)
    assert approved.status_code == 200, approved.text
    approved = approved.json()
    assert approved["review"]["resume"]["result_json"]["status"] == "APPROVED"
    assert len(approved["outputs"]) == 1
    output = approved["outputs"][0]
    assert output["target_type"] == kind and output["created_by"] == "service-owner"
    after = effects(client)
    assert decide(client, waiting).json() == approved and effects(client) == after
    assert client.get(path(episode["project_id"]) + f"/{saved['id']}").json() == approved
    if kind != "VISUAL_BIBLE":
        asset = client.get(
            f"/api/v1/projects/{episode['project_id']}/assets/{output['asset_id']}"
        ).json()
        assert asset["name"] == "雨夜角色" and asset["current_version_id"] is None
        assert output["before_json"] is None and output["content_json"]["asset_id"] == asset["id"]
    else:
        bible = client.get(f"/api/v1/projects/{episode['project_id']}/visual-bible").json()
        assert (
            output["visual_bible_version_id"] == bible["id"]
            and bible["fields"]["palette"] == "蓝灰"
        )


def test_reject_preserves_original_assets_and_records_actor_comment_and_history(client, episode):
    payload, asset = existing_payload(client, episode)
    saved, _ = save(client, episode, payload=payload)
    waiting = submit(client, saved).json()
    counts = effects(client)
    rejected = decide(client, waiting, "REJECT")
    assert rejected.status_code == 200, rejected.text
    value = rejected.json()
    assert (
        value["outputs"] == [] and value["review"]["resume"]["result_json"]["status"] == "REJECTED"
    )
    assert value["review"]["decision"]["decision_json"]["comment"] == "保留原设定"
    assert value["review"]["decision"]["actor_id"] == "service-owner"
    assert effects(client)["art_approved_versions"] == counts["art_approved_versions"]
    assert (
        client.get(f"/api/v1/projects/{episode['project_id']}/assets/{asset['id']}").json() == asset
    )
    assert decide(client, waiting).status_code == 409


def test_crud_revisions_cas_idempotency_and_soft_delete_keep_exact_history(client, episode):
    saved, body = save(client, episode)
    endpoint = path(episode["project_id"]) + f"/{saved['id']}"
    old = deepcopy(saved["version"])
    body["creation_key"] = str(uuid4())
    body["expected_revision"] = saved["revision"]
    body["proposal"]["visual_goal"] = "修订后的目标"
    revised = client.put(endpoint, json=body)
    assert revised.status_code == 201, revised.text
    revised = revised.json()
    assert revised["revision"] == 1 and revised["version"]["version_no"] == 2
    assert client.put(endpoint, json=body).status_code == 200
    assert client.get(endpoint + "/versions?limit=1").json() == [revised["version"]]
    assert client.get(endpoint + "/versions?limit=1&offset=1").json() == [old]
    assert client.get(endpoint + f"/versions/{old['id']}").json()["version"] == old
    conflict = {**body, "creation_key": str(uuid4())}
    assert client.put(endpoint, json=conflict).status_code == 409
    deleted = client.delete(endpoint + "?expected_revision=1")
    assert deleted.status_code == 200 and deleted.json()["archived"]
    assert client.get(path(episode["project_id"])).json() == []
    assert len(client.get(path(episode["project_id"]) + "?include_archived=true").json()) == 1
    assert client.get(endpoint + "/versions").json() == [revised["version"], old]
    assert submit(client, deleted.json()).status_code == 409
    restored = client.patch(endpoint, json={"expected_revision": 2, "archived": False})
    assert restored.status_code == 200 and restored.json()["revision"] == 3
    assert client.get(endpoint + "/versions").json() == [revised["version"], old]


@pytest.mark.parametrize(
    "mutation", ["missing", "hash", "option", "fields", "version", "revision", "request"]
)
def test_exact_approval_rejects_incomplete_stale_or_other_confirmation_without_mutation(
    client, episode, mutation
):
    saved, _ = save(client, episode)
    waiting = submit(client, saved).json()
    body = decision(waiting)
    if mutation == "missing":
        body.pop("confirmation")
    elif mutation == "hash":
        body["confirmation"]["comparison_sha256"] = "0" * 64
    elif mutation == "option":
        body["confirmation"]["option_id"] = str(uuid4())
    elif mutation == "fields":
        body["confirmation"]["confirmed_changes"] = []
    elif mutation == "version":
        body["version_id"] = str(uuid4())
    elif mutation == "revision":
        body["expected_revision"] += 1
    else:
        body["request_sha256"] = "0" * 64
    before = effects(client)
    assert decide(client, waiting, body=body).status_code in {409, 422}
    assert effects(client) == before


def test_formal_existing_design_versions_keep_full_before_after_and_media_identity(client, episode):
    payload, asset = existing_payload(client, episode)
    saved, _ = save(client, episode, payload=payload)
    waiting = submit(client, saved).json()
    response = decide(client, waiting)
    assert response.status_code == 200, response.text
    output = response.json()["outputs"][0]
    assert output["before_json"]["name"] == "原设定"
    assert output["content_json"]["name"] == "雨夜角色"
    assert output["before_json"]["revision"] == 0 and output["version_no"] == 1
    current = client.get(f"/api/v1/projects/{episode['project_id']}/assets/{asset['id']}").json()
    assert current["revision"] == 1 and current["current_version_id"] == asset["current_version_id"]
    assert (
        saved["version"]["baseline_json"][payload["options"][0]["designs"][0]["design_id"]]
        == output["before_json"]
    )


def test_unified_decision_cannot_bypass_explicit_art_confirmation(client, episode):
    saved, _ = save(client, episode)
    waiting = submit(client, saved).json()
    review = waiting["review"]
    response = client.post(
        f"/api/v1/human-reviews/{review['id']}/decisions",
        json={
            "version_id": saved["version"]["id"],
            "request_sha256": review["request_sha256"],
            "decision": "APPROVE",
        },
    )
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "ART_CONFIRMATION_REQUIRED"
    )
    with client.app.state.session_factory() as db:
        assert db.scalar(select(HumanReviewDecision)) is None


def test_approved_decision_can_resume_after_separate_http_restart_and_is_once_only(client, episode):
    from arclight.domain.versions import digest

    saved, _ = save(client, episode)
    waiting = submit(client, saved).json()
    review = waiting["review"]
    confirmed = decision(waiting)
    response = client.post(
        f"/api/v1/human-reviews/{review['id']}/decisions",
        json={
            "version_id": saved["version"]["id"],
            "request_sha256": review["request_sha256"],
            "decision": "APPROVE",
            "impact_digest": digest(confirmed["confirmation"]),
        },
    )
    assert response.status_code == 200, response.text
    assert effects(client)["art_approved_versions"] == 0
    body = {"decision_id": response.json()["id"], "request_sha256": review["request_sha256"]}
    endpoint = f"/api/v1/human-reviews/{review['id']}/resume"
    # A new FastAPI application and pool read only the committed review journal.
    from starlette.testclient import TestClient

    from arclight.main import create_app

    with TestClient(create_app(client.app.state.settings)) as restarted:
        restarted.headers.update(client.headers)
        resumed = restarted.post(endpoint, json=body)
        assert restarted.post(endpoint, json=body).json() == resumed.json()
    assert resumed.status_code == 200, resumed.text
    before = effects(client)
    assert client.post(endpoint, json=body).json() == resumed.json() and effects(client) == before


def test_selected_second_option_and_multi_target_approval_share_one_atomic_review(client, episode):
    payload = proposal_payload("STYLE", episode["project_id"])
    second = proposal_payload("CHARACTER", episode["project_id"])
    scene = proposal_payload("SCENE", episode["project_id"])
    payload["sources"] += second["sources"] + scene["sources"]
    option = second["options"][0]
    option["designs"] += scene["options"][0]["designs"]
    option["prompts"] += scene["options"][0]["prompts"]
    option["source_ids"] += scene["options"][0]["source_ids"]
    payload["options"].append(option)
    saved, _ = save(client, episode, payload=payload)
    waiting = submit(client, saved, option=1).json()
    body = decision(waiting)
    body["confirmation"] = acknowledgement(
        ArtComparison.model_validate(waiting["version"]["comparison_json"]), 1
    ).model_dump(mode="json")
    approved = decide(client, waiting, body=body)
    assert approved.status_code == 200, approved.text
    assert {output["target_type"] for output in approved.json()["outputs"]} == {
        "CHARACTER",
        "SCENE",
    }
    assert effects(client)["human_review_resumes"] == 1


def test_empty_changes_and_unresolved_questions_cannot_be_submitted_for_formal_approval(
    client, episode
):
    payload, asset = existing_payload(client, episode, "STYLE")
    design = payload["options"][0]["designs"][0]
    design["name"] = asset["name"]
    design.pop("metadata")
    saved, _ = save(client, episode, payload=payload)
    assert submit(client, saved).status_code == 409
    payload["questions"] = [{"question_id": str(uuid4()), "question": "需补充参考吗？"}]
    payload["options"][0]["designs"][0]["name"] = "新方案"
    saved, _ = save(client, episode, payload=payload)
    assert submit(client, saved).status_code == 409


@pytest.mark.parametrize("changed", ["directory", "new_revision", "archive"])
def test_pending_review_rechecks_changed_baseline_revision_and_archive(client, episode, changed):
    payload, asset = existing_payload(client, episode)
    saved, body = save(client, episode, payload=payload)
    waiting = submit(client, saved).json()
    endpoint = path(episode["project_id"]) + f"/{saved['id']}"
    if changed == "directory":
        response = client.put(
            f"/api/v1/projects/{episode['project_id']}/assets/{asset['id']}",
            json={"name": "已更新", "expected_revision": 0},
        )
    elif changed == "new_revision":
        body["creation_key"] = str(uuid4())
        body["proposal"]["visual_goal"] = "新的修订"
        response = client.put(endpoint, json=body)
    else:
        response = client.delete(endpoint + "?expected_revision=0")
    assert response.status_code < 300, response.text
    before = effects(client)
    assert decide(client, waiting).status_code == 409 and effects(client) == before


def test_locked_visual_bible_changes_are_owner_confirmed_without_unlocking_or_overwriting_history(
    client, episode
):
    from test_visual_bible import edit, lock

    project = {"id": episode["project_id"]}
    original = edit(client, project, palette="黑白").json()
    locked = lock(client, project, original["version_no"], ["palette"]).json()
    saved, _ = save(client, episode, "VISUAL_BIBLE")
    assert saved["version"]["comparison_json"]["options"][0]["changes"][0]["protection"] == "LOCKED"
    waiting = submit(client, saved).json()
    approved = decide(client, waiting)
    assert approved.status_code == 200, approved.text
    current = client.get(f"/api/v1/projects/{episode['project_id']}/visual-bible").json()
    assert current["fields"]["palette"] == "蓝灰" and current["locked_fields"] == ["palette"]
    assert current["version_no"] == locked["version_no"] + 1
    assert (
        client.get(
            f"/api/v1/projects/{episode['project_id']}/visual-bible/versions/{locked['id']}"
        ).json()
        == locked
    )
    assert edit(client, project, current["version_no"], palette="红").status_code == 409


def test_approval_failure_rolls_back_all_versions_decision_audit_and_receipt(
    client, episode, monkeypatch
):
    from arclight.application import art_approvals
    from arclight.core.errors import DomainError

    saved, _ = save(client, episode)
    waiting = submit(client, saved).json()
    before = effects(client)
    original = art_approvals.audit

    def fail(db, *args, **kwargs):
        original(db, *args, **kwargs)
        raise DomainError("TEST_ROLLBACK", "验收事务回滚", 409)

    with monkeypatch.context() as patch:
        patch.setattr(art_approvals, "audit", fail)
        assert decide(client, waiting).status_code == 409
    assert effects(client) == before
    assert decide(client, waiting).status_code == 200
