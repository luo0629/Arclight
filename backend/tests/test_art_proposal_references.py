"""Real isolated project/version validation leaves assets, locks and history untouched."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_art_proposals import proposal_payload
from test_collaboration import create_project, register
from test_text_contract_v2 import make_asset, save_script
from test_visual_bible import edit, lock

from arclight.application.art_proposals import (
    asset_baseline,
    validate_art_proposal,
    visual_version_sha256,
)
from arclight.application.visual_bible import snapshot
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import ArtProposal, proposal_schema_registration
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AssetSelectionLock,
    AssetVersion,
    AuditEvent,
    Episode,
    Project,
    ProjectMember,
    Script,
    VisualAsset,
    VisualBibleVersion,
    WorkflowRun,
)


def bound_payload(client, episode, kind="CHARACTER"):
    payload = proposal_payload(kind, project_id=episode["project_id"])
    payload["episode_id"] = episode["id"]
    with client.app.state.session_factory.begin() as db:
        target = make_asset(db, episode["project_id"], kind)
        image = make_asset(db, episode["project_id"], "IMAGE")
        baseline = asset_baseline(db.get(VisualAsset, UUID(target["asset_id"])))
    source, reference = str(uuid4()), str(uuid4())
    payload["origin"] = "AGENT"
    payload["sources"].append(
        {
            "kind": "VERSION",
            "source_id": source,
            "resource_type": "ASSET_VERSION",
            "resource_id": image["asset_version_id"],
            "content_sha256": "a" * 64,
        }
    )
    payload["references"] = [
        {
            "reference_id": reference,
            "source_id": source,
            "asset": image,
            "asset_type": "IMAGE",
            "purpose": "保留轮廓与色彩",
        }
    ]
    option = payload["options"][0]
    option["designs"][0]["target"] = baseline.model_dump(mode="json")
    option["reference_ids"] = [reference]
    option["source_ids"].append(source)
    option["prompts"][0]["reference_ids"] = [reference]
    option["prompts"][0]["source_ids"].append(source)
    return payload, target, image


def effects(db):
    return tuple(
        db.scalar(select(func.count()).select_from(model))
        for model in (
            VisualAsset,
            AssetVersion,
            AssetSelectionLock,
            VisualBibleVersion,
            WorkflowRun,
            AuditEvent,
        )
    )


@pytest.mark.parametrize("kind", ["CHARACTER", "SCENE", "PROP", "STYLE"])
def test_real_art_validation_is_read_only_and_preserves_input_and_old_asset_paths(
    client, episode, kind
):
    payload, target, _ = bound_payload(client, episode, kind)
    original = deepcopy(payload)
    with client.app.state.session_factory.begin() as db:
        before = effects(db)
        asset = db.get(VisualAsset, UUID(target["asset_id"]))
        base = asset_baseline(asset)
        validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        assert not db.new and not db.dirty and not db.deleted
        assert effects(db) == before and asset_baseline(asset) == base
    assert payload == original
    row = client.get(f"/api/v1/projects/{episode['project_id']}/assets/{target['asset_id']}").json()
    assert row["name"] == kind and row["revision"] == 0


@pytest.mark.parametrize(
    "fault,code",
    [
        ("checksum", "ART_SOURCE_CHANGED"),
        ("owner", "INVALID_ASSET_REFERENCE"),
        ("type", "INVALID_ASSET_REFERENCE"),
        ("draft", "ASSET_NOT_USABLE"),
        ("stale", "ASSET_NOT_USABLE"),
        ("base_hash", "ART_PROPOSAL_BASE_STALE"),
        ("revision", "ART_PROPOSAL_BASE_STALE"),
        ("lock", "ART_PROPOSAL_BASE_STALE"),
        ("target_type", "ART_TARGET_INVALID"),
        ("archived", "ASSET_NOT_USABLE"),
        ("episode", "ART_SCOPE_INVALID"),
    ],
)
def test_art_references_and_bases_reject_wrong_or_stale_identity(client, episode, fault, code):
    payload, target, image = bound_payload(client, episode)
    design = payload["options"][0]["designs"][0]
    with client.app.state.session_factory.begin() as db:
        if fault == "checksum":
            payload["sources"][-1]["content_sha256"] = "f" * 64
        elif fault == "owner":
            payload["references"][0]["asset"]["asset_id"] = str(uuid4())
        elif fault == "type":
            payload["references"][0]["asset_type"] = "SCENE"
        elif fault == "draft":
            db.get(AssetVersion, UUID(image["asset_version_id"])).status = "DRAFT"
        elif fault == "stale":
            db.get(AssetVersion, UUID(image["asset_version_id"])).stale = True
        elif fault == "base_hash":
            design["target"]["content_sha256"] = "f" * 64
        elif fault in {"revision", "lock"}:
            design["target"]["revision" if fault == "revision" else "lock_version"] += 1
        elif fault == "target_type":
            wrong = make_asset(db, episode["project_id"], "PROP")
            design["target"] = asset_baseline(
                db.get(VisualAsset, UUID(wrong["asset_id"]))
            ).model_dump(mode="json")
        elif fault == "archived":
            db.get(VisualAsset, UUID(target["asset_id"])).status = "ARCHIVED"
        else:
            payload["episode_id"] = str(uuid4())
    with client.app.state.session_factory.begin() as db:
        before = effects(db)
        with pytest.raises(DomainError) as error:
            validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        assert error.value.code == code and effects(db) == before
        assert not db.new and not db.dirty and not db.deleted


def test_candidate_comparison_does_not_promote_a_draft_to_approved_reference(client, episode):
    payload, _, image = bound_payload(client, episode)
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(image["asset_version_id"])).status = "DRAFT"
    payload["references"][0]["usage"] = "COMPARISON"
    with client.app.state.session_factory.begin() as db:
        validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        assert db.get(AssetVersion, UUID(image["asset_version_id"])).status == "DRAFT"
        assert not db.dirty and not db.new


def test_cross_project_references_and_unauthorized_validator_are_rejected(client, episode):
    payload, _, _ = bound_payload(client, episode)
    other = create_project(client)
    with client.app.state.session_factory.begin() as db:
        foreign = make_asset(db, other["id"], "IMAGE")
    payload["references"][0]["asset"] = foreign
    payload["sources"][-1]["resource_id"] = foreign["asset_version_id"]
    with (
        client.app.state.session_factory.begin() as db,
        pytest.raises(DomainError, match="不属于当前项目"),
    ):
        validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
    outsider = register(client, "art-outsider@example.test")
    payload, _, _ = bound_payload(client, episode)
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as error:
            validate_art_proposal(db, ArtProposal.model_validate(payload), outsider["user"]["id"])
        assert error.value.code == "NOT_FOUND"


@pytest.mark.parametrize("legacy", [True, False])
def test_visual_proposal_preserves_locks_partial_fields_and_rejects_expired_base(
    client, episode, legacy
):
    project = {"id": episode["project_id"]}
    if not legacy:
        assert edit(client, project, palette="暖橙").status_code == 201
        assert lock(client, project, 1, ["palette"]).status_code == 201
    payload = proposal_payload("VISUAL_BIBLE", episode["project_id"])
    design = payload["options"][0]["designs"][0]
    with client.app.state.session_factory.begin() as db:
        current = snapshot(db, db.get(Project, UUID(project["id"])))
        design.update(
            version_id=current["version_id"],
            version_no=current["version_no"],
            content_sha256=digest(current),
        )
        if not legacy:
            row = db.get(VisualBibleVersion, UUID(current["version_id"]))
            assert visual_version_sha256(row) == digest(current)
            payload["lock_conflicts"] = [
                {
                    "design_id": design["design_id"],
                    "field_path": "fields.palette",
                    "reason": "须人工确认配色变更",
                }
            ]
        proposal = ArtProposal.model_validate(payload)
        before = effects(db)
        validate_art_proposal(db, proposal, "service-owner")
        assert effects(db) == before and not db.dirty
    assert (
        edit(client, project, expected=current["version_no"], lighting="冷月光").status_code == 201
    )
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as error:
            validate_art_proposal(db, proposal, "service-owner")
        assert error.value.code == "ART_PROPOSAL_BASE_STALE"
    assert ArtProposal.model_validate_json(proposal.model_dump_json()) == proposal


def test_approved_script_source_is_exact_and_stale_script_cannot_be_reused(client, episode):
    saved = save_script(client, episode)
    payload = proposal_payload(project_id=episode["project_id"])
    payload.update(origin="AGENT", episode_id=episode["id"])
    source_id = str(uuid4())
    with client.app.state.session_factory.begin() as db:
        source = db.get(Script, UUID(saved["id"]))
        payload["sources"].append(
            {
                "kind": "VERSION",
                "source_id": source_id,
                "resource_type": "SCRIPT",
                "resource_id": saved["id"],
                "content_sha256": digest(source.content_json),
            }
        )
        payload["options"][0]["source_ids"].append(source_id)
        validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        db.get(AssetVersion, source.id).stale = True
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as error:
            validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        assert error.value.code == "ASSET_NOT_USABLE"


def test_actual_art_schema_registers_and_preserves_exact_definition_and_legacy_outer(
    client, episode
):
    body = proposal_schema_registration().model_dump(mode="json")
    url = f"/api/v1/projects/{episode['project_id']}/production-versions"
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    original = response.json()
    assert client.post(url, json=body).json()["id"] == original["id"]
    changed = deepcopy(body)
    changed["definition"]["authority"] = "future-incompatible-definition"
    replacement = client.post(url, json=changed).json()
    assert replacement["id"] != original["id"] and replacement["digest"] != original["digest"]
    assert client.get(url + "/" + original["id"]).json() == original
    assert client.get(url + "/bindings").json() == []


@pytest.mark.parametrize("new", [True, False])
def test_standard_character_image_requires_approval_even_for_explicit_comparison(
    client, episode, new
):
    payload, _, image = bound_payload(client, episode)
    design = payload["options"][0]["designs"][0]
    if new:
        design["target"] = {"mode": "NEW"}
    image_id = str(uuid4())
    design["profile"] = {
        "images": [{"id": image_id, "label": "正面", "kind": "INITIAL", "source": image}],
        "standard_image_id": image_id,
    }
    proposal = ArtProposal.model_validate(payload)
    with client.app.state.session_factory.begin() as db:
        validate_art_proposal(db, proposal, "service-owner")
        db.get(AssetVersion, UUID(image["asset_version_id"])).status = "DRAFT"
    payload["references"][0]["usage"] = "COMPARISON"
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as error:
            validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        assert error.value.code == "STANDARD_IMAGE_NOT_APPROVED"
        design["profile"]["standard_image_id"] = None
        validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        assert not db.new and not db.dirty and not db.deleted


@pytest.mark.parametrize(
    "kind,foreign", [("SCENE", False), ("SCENE", True), ("PROP", False), ("PROP", True)]
)
def test_scene_and_prop_profiles_reuse_existing_reference_authority(client, episode, kind, foreign):
    payload, target, image = bound_payload(client, episode, kind)
    with client.app.state.session_factory.begin() as db:
        if kind == "SCENE":
            selected = make_asset(db, episode["project_id"], "SCENE") if foreign else target
            selected_kind = "SCENE"
        else:
            selected = image if foreign else make_asset(db, episode["project_id"], "CHARACTER")
            selected_kind = "IMAGE" if foreign else "CHARACTER"
    payload["references"][0].update(asset=selected, asset_type=selected_kind)
    payload["sources"][-1]["resource_id"] = selected["asset_version_id"]
    design = payload["options"][0]["designs"][0]
    design["profile"]["reference" if kind == "SCENE" else "holder"] = selected
    with client.app.state.session_factory.begin() as db:
        if foreign:
            with pytest.raises(DomainError) as error:
                validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
            assert error.value.code == "INVALID_ASSET_REFERENCE"
        else:
            validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        assert not db.new and not db.dirty and not db.deleted
    payload["options"][0]["reference_ids"] = []
    with pytest.raises(ValidationError, match="Profile references"):
        ArtProposal.model_validate(payload)


@pytest.mark.parametrize("fault", [None, "hash", "foreign"])
def test_exact_visual_bible_source_is_historical_and_project_bound(client, episode, fault):
    project = {"id": episode["project_id"]}
    if fault == "foreign":
        project = create_project(client)
    assert edit(client, project, palette="暖橙").status_code == 201
    payload = proposal_payload(project_id=episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        bible = db.scalar(
            select(VisualBibleVersion).where(VisualBibleVersion.project_id == UUID(project["id"]))
        )
        source_id = str(uuid4())
        payload["sources"].append(
            {
                "kind": "VERSION",
                "source_id": source_id,
                "resource_type": "VISUAL_BIBLE",
                "resource_id": str(bible.id),
                "content_sha256": "f" * 64 if fault == "hash" else visual_version_sha256(bible),
            }
        )
        payload["options"][0]["source_ids"].append(source_id)
    assert edit(client, project, expected=1, lighting="冷月光").status_code == 201
    with client.app.state.session_factory.begin() as db:
        if fault:
            with pytest.raises(DomainError) as error:
                validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
            assert error.value.code == (
                "ART_SOURCE_CHANGED" if fault == "hash" else "ART_SOURCE_INVALID"
            )
        else:
            validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        assert not db.new and not db.dirty and not db.deleted


@pytest.mark.parametrize("fault", ["unapproved", "type_alias", "other_episode"])
def test_script_source_cannot_bypass_approval_type_or_episode_scope(client, episode, fault):
    saved = save_script(client, episode, approve=fault != "unapproved")
    payload = proposal_payload(project_id=episode["project_id"])
    payload["episode_id"] = episode["id"]
    with client.app.state.session_factory.begin() as db:
        script = db.get(Script, UUID(saved["id"]))
        source_id = str(uuid4())
        payload["sources"].append(
            {
                "kind": "VERSION",
                "source_id": source_id,
                "resource_type": "ASSET_VERSION" if fault == "type_alias" else "SCRIPT",
                "resource_id": saved["id"],
                "content_sha256": digest(script.content_json),
            }
        )
        payload["options"][0]["source_ids"].append(source_id)
        if fault == "other_episode":
            other = Episode(project_id=UUID(episode["project_id"]), episode_no=2, title="次集")
            db.add(other)
            db.flush()
            payload["episode_id"] = str(other.id)
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as error:
            validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        assert error.value.code == "ART_SOURCE_INVALID"


@pytest.mark.parametrize(
    "scope,code",
    [
        ("project", "PROJECT_ARCHIVED"),
        ("episode", "EPISODE_ARCHIVED"),
        ("usage", "INVALID_ASSET_SCOPE"),
    ],
)
def test_art_scope_rejects_archives_and_foreign_usage(client, episode, scope, code):
    payload, _, _ = bound_payload(client, episode)
    with client.app.state.session_factory.begin() as db:
        if scope == "project":
            db.get(Project, UUID(episode["project_id"])).archived = True
        elif scope == "episode":
            db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
        else:
            payload["options"][0]["designs"][0]["metadata"]["usage_scope"] = {
                "kind": "EPISODES",
                "episode_ids": [str(uuid4())],
            }
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as error:
            validate_art_proposal(db, ArtProposal.model_validate(payload), "service-owner")
        assert error.value.code == code


def test_read_only_validator_rechecks_revoked_membership_without_granting_write(client, episode):
    payload, _, _ = bound_payload(client, episode)
    viewer = register(client, "art-viewer@example.test")["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        member = ProjectMember(
            project_id=UUID(episode["project_id"]), user_id=UUID(viewer), role="viewer"
        )
        db.add(member)
        db.flush()
        validate_art_proposal(db, ArtProposal.model_validate(payload), viewer)
        db.delete(member)
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as error:
            validate_art_proposal(db, ArtProposal.model_validate(payload), viewer)
        assert error.value.code == "NOT_FOUND"
