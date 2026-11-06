"""ART-03 real manual/model preflight and exact, read-only Owner acknowledgement."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_art_proposal_references import bound_payload, effects
from test_art_proposals import proposal_payload
from test_asset_locks import lock as selection_lock
from test_asset_locks import selected
from test_asset_versions import private_objects  # noqa: F401
from test_assets import rename
from test_collaboration import create_episode, create_project, register
from test_visual_bible import edit, lock

from arclight.application.art_proposals import asset_baseline
from arclight.application.art_protection import compare_art_proposal, require_art_confirmation
from arclight.application.visual_bible import snapshot
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import ArtProposal
from arclight.domain.art_protection import ArtComparison, ArtConfirmation
from arclight.domain.versions import digest
from arclight.persistence.models import AssetSelectionLock, Project, ProjectMember, VisualAsset


def endpoint(project_id):
    return f"/api/v1/projects/{project_id}/art-proposals/compare"


def compare(client, payload):
    return client.post(endpoint(payload["project_id"]), json=payload)


def acknowledgement(report, index=0):
    option = report.options[index]
    return ArtConfirmation(
        comparison_sha256=report.comparison_sha256,
        option_id=option.option_id,
        confirmed_changes=[
            {"design_id": change.design_id, "field_path": change.field_path}
            for change in option.changes
        ],
    )


def bible_payload(client, episode, **fields):
    payload = proposal_payload("VISUAL_BIBLE", episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        current = snapshot(db, db.get(Project, UUID(episode["project_id"])))
    payload["options"][0]["designs"][0].update(
        version_id=current["version_id"],
        version_no=current["version_no"],
        content_sha256=digest(current),
        fields=fields,
    )
    return payload


def refresh_target(client, payload):
    design = payload["options"][0]["designs"][0]
    with client.app.state.session_factory.begin() as db:
        design["target"] = asset_baseline(
            db.get(VisualAsset, UUID(design["target"]["asset_id"]))
        ).model_dump(mode="json")


@pytest.mark.parametrize("origin", ["MANUAL", "AGENT"])
def test_manual_and_model_proposals_share_real_api_without_mutations(client, episode, origin):
    payload, target, _ = bound_payload(client, episode)
    payload["origin"] = origin
    original = deepcopy(payload)
    with client.app.state.session_factory.begin() as db:
        before = effects(db)
        baseline = asset_baseline(db.get(VisualAsset, UUID(target["asset_id"])))
    response = compare(client, payload)
    assert response.status_code == 200, response.text
    report = ArtComparison.model_validate(response.json())
    assert report.requires_human_review and report.proposal_sha256 == digest(
        ArtProposal.model_validate(payload).model_dump(mode="json")
    )
    assert compare(client, payload).json() == response.json()
    with client.app.state.session_factory.begin() as db:
        checked = require_art_confirmation(
            db, ArtProposal.model_validate(payload), "service-owner", acknowledgement(report)
        )
        assert checked == report.options[0]
        assert (
            effects(db) == before
            and asset_baseline(db.get(VisualAsset, UUID(target["asset_id"]))) == baseline
        )
        assert not db.new and not db.dirty and not db.deleted
    assert payload == original


@pytest.mark.parametrize("kind", ["CHARACTER", "SCENE", "PROP", "STYLE", "VISUAL_BIBLE"])
def test_new_visual_concepts_and_legacy_bible_have_explicit_confirmation_scope(
    client, episode, kind
):
    payload = (
        bible_payload(client, episode, palette="蓝灰")
        if kind == "VISUAL_BIBLE"
        else proposal_payload(kind, episode["project_id"])
    )
    response = compare(client, payload)
    assert response.status_code == 200, response.text
    report = ArtComparison.model_validate(response.json())
    assert len(report.options[0].changes) == 1
    change = report.options[0].changes[0]
    assert change.target_type == kind and change.target_id is None and change.protection == "NONE"
    assert change.field_path == ("fields.palette" if kind == "VISUAL_BIBLE" else "asset")
    with client.app.state.session_factory.begin() as db:
        assert require_art_confirmation(
            db, ArtProposal.model_validate(payload), "service-owner", acknowledgement(report)
        )
        assert not db.new and not db.dirty


def test_bible_field_locks_are_authoritative_and_exact_text_is_preserved(client, episode):
    project = {"id": episode["project_id"]}
    assert edit(client, project, palette=" 蓝灰 ", lighting="月光").status_code == 201
    assert lock(client, project, 1, ["palette"]).status_code == 201
    payload = bible_payload(client, episode, palette=" 新色 ", lighting="暖灯")
    design = payload["options"][0]["designs"][0]
    # A self-declared conflict on an unlocked field cannot conceal the real palette lock.
    payload["lock_conflicts"] = [
        {
            "design_id": design["design_id"],
            "field_path": "fields.lighting",
            "reason": "模型猜测的锁",
        }
    ]
    response = compare(client, payload)
    assert response.status_code == 200, response.text
    report = ArtComparison.model_validate(response.json())
    changes = {change.field_path: change for change in report.options[0].changes}
    assert changes["fields.palette"].protection == "LOCKED"
    assert changes["fields.lighting"].protection == "NONE"
    assert (
        changes["fields.palette"].before == " 蓝灰 "
        and changes["fields.palette"].proposed == " 新色 "
    )
    assert ArtComparison.model_validate_json(report.model_dump_json()) == report
    with client.app.state.session_factory.begin() as db:
        before = effects(db)
        require_art_confirmation(
            db, ArtProposal.model_validate(payload), "service-owner", acknowledgement(report)
        )
        assert effects(db) == before and not db.new and not db.dirty
    assert edit(client, project, expected=2, palette="静默覆盖").status_code == 409
    assert client.get(f"/api/v1/projects/{project['id']}/visual-bible").json()["locked_fields"] == [
        "palette"
    ]


def test_unchanged_locked_bible_field_is_not_a_conflict_or_an_empty_approval(client, episode):
    project = {"id": episode["project_id"]}
    edit(client, project, palette="蓝灰")
    lock(client, project, 1, ["palette"])
    payload = bible_payload(client, episode, palette="蓝灰")
    report = ArtComparison.model_validate(compare(client, payload).json())
    assert report.options[0].changes == []
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as error:
            require_art_confirmation(
                db, ArtProposal.model_validate(payload), "service-owner", acknowledgement(report)
            )
        assert error.value.code == "NO_ART_CHANGE"


def test_real_selection_lock_is_preserved_and_existing_write_api_stays_blocked(client, episode):
    row, version = selected(client, episode["project_id"])
    locked = selection_lock(client, row, version).json()
    payload = proposal_payload("CHARACTER", episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        asset = db.get(VisualAsset, UUID(row["id"]))
        payload["options"][0]["designs"][0]["target"] = asset_baseline(asset).model_dump(
            mode="json"
        )
        baseline = asset_baseline(asset)
        before = effects(db)
    response = compare(client, payload)
    assert response.status_code == 200, response.text
    report = ArtComparison.model_validate(response.json())
    assert report.options[0].changes and all(
        change.protection == "LOCKED" for change in report.options[0].changes
    )
    assert "metadata.description" in {change.field_path for change in report.options[0].changes}
    with client.app.state.session_factory.begin() as db:
        require_art_confirmation(
            db, ArtProposal.model_validate(payload), "service-owner", acknowledgement(report)
        )
        assert db.scalar(select(AssetSelectionLock)).version_id == UUID(version["id"])
        assert (
            effects(db) == before
            and asset_baseline(db.get(VisualAsset, UUID(row["id"]))) == baseline
        )
        assert not db.new and not db.dirty and not db.deleted
    assert rename(client, locked).status_code == 409


@pytest.mark.parametrize("kind", ["CHARACTER", "SCENE", "PROP", "STYLE"])
def test_existing_approved_assets_report_only_actual_fields_and_preserve_defaults(
    client, episode, kind
):
    payload, _, _ = bound_payload(client, episode, kind)
    response = compare(client, payload)
    assert response.status_code == 200, response.text
    changes = response.json()["options"][0]["changes"]
    assert {change["field_path"] for change in changes} == (
        {"name", "metadata.description", "profile"}
        if kind != "STYLE"
        else {"name", "metadata.description"}
    )
    assert all(change["protection"] == "APPROVED" for change in changes)


def test_removing_standard_reference_and_images_remains_visible_in_confirmation(client, episode):
    payload, target, image = bound_payload(client, episode)
    image_id = str(uuid4())
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(target["asset_id"])).character_profile = {
            "images": [{"id": image_id, "label": "正面", "kind": "INITIAL", "source": image}],
            "standard_image_id": image_id,
        }
    refresh_target(client, payload)
    payload["options"][0]["designs"][0]["profile"] = {"fixed_features": ["黑发"]}
    report = ArtComparison.model_validate(compare(client, payload).json())
    changes = {change.field_path: change for change in report.options[0].changes}
    assert changes["profile.images"].before and changes["profile.images"].proposed == []
    assert (
        changes["profile.standard_image_id"].before == image_id
        and changes["profile.standard_image_id"].proposed is None
    )
    proof = acknowledgement(report)
    proof.confirmed_changes = [
        item for item in proof.confirmed_changes if item.field_path != "profile.standard_image_id"
    ]
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as error:
            require_art_confirmation(
                db, ArtProposal.model_validate(payload), "service-owner", proof
            )
        assert error.value.code == "ART_CONFIRMATION_REQUIRED"


@pytest.mark.parametrize(
    "key,value,path",
    [
        pytest.param("legacy_flag", {"keep": True}, "metadata.legacy_flag", id="field"),
        pytest.param("legacy-key", {"keep": True}, "metadata", id="invalid-path"),
        pytest.param("legacy_null", None, "metadata", id="null-removal"),
        pytest.param("x" * 170, "legacy", "metadata", id="long-path"),
    ],
)
def test_legacy_metadata_removal_is_visible_without_modifying_the_original(
    client, episode, key, value, path
):
    payload, target, _ = bound_payload(client, episode, "STYLE")
    stored = {"description": "旧内容", key: value}
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(target["asset_id"])).metadata_json = stored
    refresh_target(client, payload)
    report = ArtComparison.model_validate(compare(client, payload).json())
    paths = {change.field_path for change in report.options[0].changes}
    assert path in paths
    with client.app.state.session_factory.begin() as db:
        require_art_confirmation(
            db, ArtProposal.model_validate(payload), "service-owner", acknowledgement(report)
        )
        assert db.get(VisualAsset, UUID(target["asset_id"])).metadata_json == stored
        assert not db.dirty


@pytest.mark.parametrize(
    "fault,code",
    [
        ("hash", "ART_COMPARISON_STALE"),
        ("option", "ART_OPTION_INVALID"),
        ("missing", "ART_CONFIRMATION_REQUIRED"),
        ("extra", "ART_CONFIRMATION_REQUIRED"),
        ("design", "ART_CONFIRMATION_REQUIRED"),
        ("proposal", "ART_COMPARISON_STALE"),
        ("question", "ART_QUESTIONS_UNRESOLVED"),
    ],
)
def test_confirmation_cannot_hide_changes_retarget_or_reuse_a_different_proposal(
    client, episode, fault, code
):
    payload, _, _ = bound_payload(client, episode)
    if fault == "question":
        payload["questions"] = [{"question_id": str(uuid4()), "question": "还缺谁的确认？"}]
    report = ArtComparison.model_validate(compare(client, payload).json())
    proof = acknowledgement(report).model_dump(mode="json")
    if fault == "hash":
        proof["comparison_sha256"] = "f" * 64
    elif fault == "option":
        proof["option_id"] = str(uuid4())
    elif fault == "missing":
        proof["confirmed_changes"].pop()
    elif fault == "extra":
        proof["confirmed_changes"].append(
            {
                "design_id": payload["options"][0]["designs"][0]["design_id"],
                "field_path": "current_version_id",
            }
        )
    elif fault == "design":
        proof["confirmed_changes"][0]["design_id"] = str(uuid4())
    elif fault == "proposal":
        payload["visual_goal"] = "不同的视觉任务"
    with client.app.state.session_factory.begin() as db:
        before = effects(db)
        with pytest.raises(DomainError) as error:
            require_art_confirmation(
                db,
                ArtProposal.model_validate(payload),
                "service-owner",
                ArtConfirmation.model_validate(proof),
            )
        assert error.value.code == code and effects(db) == before
        assert not db.new and not db.dirty and not db.deleted


def test_confirmation_model_rejects_duplicate_locations_and_extra_authority(client, episode):
    payload, _, _ = bound_payload(client, episode)
    report = ArtComparison.model_validate(compare(client, payload).json())
    proof = acknowledgement(report).model_dump(mode="json")
    proof["confirmed_changes"] *= 2
    with pytest.raises(ValidationError):
        ArtConfirmation.model_validate(proof)
    proof["confirmed_changes"] = []
    proof["unlock"] = True
    with pytest.raises(ValidationError):
        ArtConfirmation.model_validate(proof)


def test_confirmation_selects_exactly_one_option_and_its_own_change_locations(client, episode):
    payload, _, _ = bound_payload(client, episode)
    alternative = deepcopy(payload["options"][0])
    alternative["option_id"] = str(uuid4())
    design_id = str(uuid4())
    alternative["designs"][0].update(design_id=design_id, name="第二套视觉方案")
    alternative["prompts"][0].update(fragment_id=str(uuid4()), design_ids=[design_id])
    payload["options"].append(alternative)
    report = ArtComparison.model_validate(compare(client, payload).json())
    assert len(report.options) == 2
    proof = acknowledgement(report)
    proof.option_id = report.options[1].option_id
    with client.app.state.session_factory.begin() as db:
        before = effects(db)
        with pytest.raises(DomainError) as error:
            require_art_confirmation(
                db, ArtProposal.model_validate(payload), "service-owner", proof
            )
        assert error.value.code == "ART_CONFIRMATION_REQUIRED"
        assert (
            require_art_confirmation(
                db, ArtProposal.model_validate(payload), "service-owner", acknowledgement(report, 1)
            )
            == report.options[1]
        )
        assert effects(db) == before and not db.new and not db.dirty and not db.deleted


@pytest.mark.parametrize("limit", ["fields", "bytes"])
def test_oversized_server_comparison_fails_closed_without_mutation(client, episode, limit):
    payload, target, _ = bound_payload(client, episode, "STYLE")
    with client.app.state.session_factory.begin() as db:
        asset = db.get(VisualAsset, UUID(target["asset_id"]))
        asset.metadata_json = (
            {f"legacy_{index}": True for index in range(513)}
            if limit == "fields"
            else {"description": "旧" * 90000}
        )
    refresh_target(client, payload)
    with client.app.state.session_factory.begin() as db:
        before = effects(db)
        baseline = asset_baseline(db.get(VisualAsset, UUID(target["asset_id"])))
    response = compare(client, payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ART_COMPARISON_TOO_LARGE"
    with client.app.state.session_factory.begin() as db:
        assert effects(db) == before
        assert asset_baseline(db.get(VisualAsset, UUID(target["asset_id"]))) == baseline


@pytest.mark.parametrize("role", ["owner", "editor", "viewer", "outsider"])
def test_compare_and_confirm_use_real_project_roles_not_model_origin(client, role):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    payload, _, _ = bound_payload(client, episode)
    report = ArtComparison.model_validate(compare(client, payload).json())
    actor = owner["user"]["id"]
    if role != "owner":
        actor = register(client, role + "@art.test")["user"]["id"]
        if role != "outsider":
            with client.app.state.session_factory.begin() as db:
                db.add(
                    ProjectMember(project_id=UUID(project["id"]), user_id=UUID(actor), role=role)
                )
    response = compare(client, payload)
    assert response.status_code == (
        200 if role in {"owner", "editor"} else 403 if role == "viewer" else 404
    )
    with client.app.state.session_factory.begin() as db:
        if role == "owner":
            require_art_confirmation(
                db, ArtProposal.model_validate(payload), actor, acknowledgement(report)
            )
        else:
            with pytest.raises(DomainError) as error:
                require_art_confirmation(
                    db, ArtProposal.model_validate(payload), actor, acknowledgement(report)
                )
            assert error.value.code == ("NOT_FOUND" if role == "outsider" else "FORBIDDEN")


@pytest.mark.parametrize("change", ["downgrade", "revoke"])
def test_cached_member_permissions_are_rechecked_after_comparison(client, change):
    register(client)
    project = create_project(client)
    payload, _, _ = bound_payload(client, create_episode(client, project))
    actor = register(client, "member@art.test")["user"]["id"]
    factory = client.app.state.session_factory
    with factory.begin() as db:
        member = ProjectMember(project_id=UUID(project["id"]), user_id=UUID(actor), role="editor")
        db.add(member)
    proposal = ArtProposal.model_validate(payload)
    with factory() as stale:
        cached_member = stale.scalar(select(ProjectMember))
        assert compare_art_proposal(stale, proposal, actor)
        stale.commit()
        with factory.begin() as db:
            member = db.scalar(select(ProjectMember))
            if change == "revoke":
                db.delete(member)
            else:
                member.role = "viewer"
        assert cached_member.role == "editor"
        assert compare(client, payload).status_code == (404 if change == "revoke" else 403)
        with pytest.raises(DomainError) as error:
            compare_art_proposal(stale, proposal, actor)
        assert error.value.code == ("NOT_FOUND" if change == "revoke" else "FORBIDDEN")
        assert not stale.new and not stale.dirty and not stale.deleted


def test_owner_confirmation_rechecks_ownership_after_comparison(client):
    owner = register(client)["user"]["id"]
    project = create_project(client)
    payload, _, _ = bound_payload(client, create_episode(client, project))
    proposal = ArtProposal.model_validate(payload)
    report = ArtComparison.model_validate(compare(client, payload).json())
    next_owner = register(client, "next-owner@art.test")["user"]["id"]
    factory = client.app.state.session_factory
    with factory() as stale:
        require_art_confirmation(stale, proposal, owner, acknowledgement(report))
        stale.commit()
        with factory.begin() as db:
            db.get(Project, UUID(project["id"])).owner_id = next_owner
            db.add(
                ProjectMember(project_id=UUID(project["id"]), user_id=UUID(owner), role="editor")
            )
        with pytest.raises(DomainError) as error:
            require_art_confirmation(stale, proposal, owner, acknowledgement(report))
        assert error.value.code == "FORBIDDEN"
        assert not stale.new and not stale.dirty and not stale.deleted


def test_api_rejects_cross_project_path_and_hidden_write_fields(client, episode):
    payload, _, _ = bound_payload(client, episode)
    foreign = create_project(client)
    response = client.post(endpoint(foreign["id"]), json=payload)
    assert response.status_code == 422 and response.json()["error"]["code"] == "ART_SCOPE_INVALID"
    payload["unlock"] = True
    assert compare(client, payload).status_code == 422


@pytest.mark.parametrize(
    "fault", ["revision", "lock_version", "content", "reference", "inconsistent_lock"]
)
def test_new_compare_rejects_stale_or_inconsistent_server_authority(client, episode, fault):
    payload, target, _ = bound_payload(client, episode)
    with client.app.state.session_factory.begin() as db:
        asset = db.get(VisualAsset, UUID(target["asset_id"]))
        if fault in {"revision", "lock_version"}:
            setattr(asset, fault, getattr(asset, fault) + 1)
        elif fault == "content":
            asset.name = "已改名"
        elif fault == "reference":
            payload["sources"][-1]["content_sha256"] = "f" * 64
        else:
            asset.status = "LOCKED"
    if fault == "inconsistent_lock":
        refresh_target(client, payload)
    response = compare(client, payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == (
        "ART_SOURCE_CHANGED"
        if fault == "reference"
        else "ART_LOCK_STATE_INVALID"
        if fault == "inconsistent_lock"
        else "ART_PROPOSAL_BASE_STALE"
    )
