"""AREV-01 exercises the real tool executor, checked candidates and stored images."""

import base64
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from storage_helpers import clear_test_bucket
from test_art_research import add_image
from test_assets import create as create_asset
from test_concurrency import require_postgres
from test_contexts import approved_board
from test_continuity_reviews import review_case  # noqa: F401
from test_director_planning import prepare
from test_generation_candidates import candidates, ingest
from test_generation_delivery import deliver
from test_generation_jobs import capture_ok, payload
from test_text_contract_v2 import make_asset, save_script

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import (
    build_agent_tool_registry,
    execute_agent_tool,
    grant_agent_tools,
)
from arclight.application.asset_objects import object_path
from arclight.application.context_evidence import read_trace
from arclight.application.context_images import load_context_images
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.agent_state import transition_agent_step
from arclight.domain.agent_tools import AgentToolCall, AgentToolGrantRequest
from arclight.domain.continuity_research import InvestigateContinuityParameters
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AssetVersion,
    GenerationAttempt,
    Project,
    ProjectMember,
    Storyboard,
    VisualAsset,
    WorkflowRun,
)
from arclight.providers.media_submission import MockMediaSubmissionProvider
from arclight.storage.objects import ObjectStorage, initialize


@pytest.fixture
def research_case(client, episode, settings, request):
    review = request.getfixturevalue("review_case")
    with client.app.state.session_factory() as db:
        board = db.get(Storyboard, UUID(review["target"]["storyboard_id"]))
        source = {"id": str(board.script_version_id)}
    if getattr(request, "param", True) == "unstarted":
        return {"actor": "service-owner", "target": review["target"], "source": source}
    run_id, actor, _ = prepare(client, episode, settings, source=source)
    if getattr(request, "param", True):
        with client.app.state.session_factory.begin() as db:
            grant_agent_tools(
                db,
                AgentToolGrantRequest(
                    run_id=run_id,
                    agent_name="continuity_reviewer",
                    allowed_tools=["investigate_continuity"],
                ),
                actor,
                build_agent_tool_registry(settings=settings),
            )
    return {"run_id": run_id, "actor": actor, "target": review["target"], "source": source}


@pytest.fixture(params=["local", "s3"])
def research_storage(request, settings, research_case):
    if request.param == "s3":
        endpoint = os.getenv("ARCLIGHT_TEST_S3_ENDPOINT")
        if not endpoint:
            pytest.skip("Requires isolated S3 test service")
        settings.object_storage_backend = "s3"
        settings.s3_endpoint_url = endpoint
        settings.s3_access_key = type(settings.s3_access_key)("arclight-test")
        settings.s3_secret_key = type(settings.s3_secret_key)("arclight-test-secret")
        settings.s3_bucket = "arclight-test-arev-" + uuid4().hex
        initialize(settings)
    try:
        yield request.param
    finally:
        if request.param == "s3":
            clear_test_bucket(ObjectStorage(settings))


def start_case(client, episode, settings, case):
    run_id, actor, _ = prepare(client, episode, settings, source=case["source"])
    with client.app.state.session_factory.begin() as db:
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=run_id,
                agent_name="continuity_reviewer",
                allowed_tools=["investigate_continuity"],
            ),
            actor,
            build_agent_tool_registry(settings=settings),
        )
    return deepcopy(case) | {"run_id": run_id, "actor": actor}


def query(client, settings, case, *, key=None, agent="continuity_reviewer", **changes):
    with client.app.state.session_factory.begin() as db:
        return execute_agent_tool(
            db,
            build_agent_tool_registry(settings=settings),
            agent,
            AgentToolCall(
                run_id=case["run_id"],
                tool_name="investigate_continuity",
                idempotency_key=key or uuid4().hex,
                arguments={"target": case["target"], **changes},
            ),
        )


def trace(client, case, through=None):
    with client.app.state.session_factory() as db:
        return read_trace(
            db, db.get(WorkflowRun, case["run_id"]), "storyboard", case["actor"], through
        )


def images(client, settings, case, file_ids, **options):
    with client.app.state.session_factory() as db:
        return load_context_images(
            db,
            settings,
            db.get(WorkflowRun, case["run_id"]),
            "storyboard",
            case["actor"],
            list(map(UUID, file_ids)),
            **options,
        )


def generated_references(
    client, episode, case, refs, dependency_type="REFERENCE_DEPENDENCY", target_type="SHOT_IMAGE"
):
    body = payload(
        inputs=[
            {"source_asset_version_id": ref["id"], "dependency_type": dependency_type}
            for ref in refs
        ]
    )
    body["target_type"] = target_type
    body["spec"]["output_spec"] = {"width": 8, "height": 12, "format": "png"}
    job = capture_ok(client, episode["project_id"], body)
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 1
    candidate = candidates(client, episode["project_id"], job)[0]
    case = deepcopy(case)
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(candidate["asset_version_id"]))
        case["target"].update(
            candidate_id=candidate["id"],
            asset_id=str(version.asset_id),
            asset_version_id=str(version.id),
            input_manifest_id=candidate["input_manifest_id"],
            media_check_sha256=digest(candidate["media_check"]),
        )
    return case


def test_checked_target_exact_shots_sources_and_missing_never_select(
    client, settings, research_case
):
    case = research_case
    before = trace(client, case)
    feedback = query(client, settings, case)
    assert feedback.status == "SUCCEEDED", feedback.model_dump_json()
    result = feedback.result
    assert result["target"] == case["target"]
    assert len(result["image_file_ids"]) == 1
    materials = result["materials"]
    assert {item["source_kind"] for item in materials} == {
        "CANDIDATE",
        "STORYBOARD",
        "SCRIPT",
        "SHOT",
        "VISUAL",
    }
    shots = [item for item in materials if item["source_kind"] == "SHOT"]
    assert [item["content"]["shot_no"] for item in shots] == [1, 2]
    assert all(
        item["content"]["storyboard_id"] == case["target"]["storyboard_id"] for item in shots
    )
    assert {item["reason"] for item in result["missing"]} >= {
        "NO_ADJACENT_SHOT",
        "NO_EXACT_REFERENCE",
    }
    after = trace(client, case)
    assert after["task_context"] == before["task_context"]
    evidence = after["evidence"][0]
    assert evidence["continuity_investigation"]["result"] == result
    assert len(evidence["sources"]) == 3
    for material in materials:
        assert material["content_sha256"] == digest(material["content"])
    loaded = images(client, settings, case, result["image_file_ids"])
    assert len(loaded) == 1 and next(iter(loaded.values())).data
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(case["target"]["asset_version_id"]))
        asset = db.get(VisualAsset, version.asset_id)
        assert version.status == "DRAFT" and asset.current_version_id is None
        assert db.get(WorkflowRun, case["run_id"]).status == "RUNNING"
    serialized = str(evidence)
    for forbidden in ("content_base64", "object_key", "https://", "data:image/"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "field",
    [
        "candidate_id",
        "asset_id",
        "asset_version_id",
        "input_manifest_id",
        "storyboard_id",
        "shot_id",
        "media_check_sha256",
    ],
)
def test_invented_or_wrong_exact_target_is_refused_atomically(
    client, settings, research_case, field
):
    target = deepcopy(research_case["target"])
    target[field] = "0" * 64 if field == "media_check_sha256" else str(uuid4())
    feedback = query(client, settings, research_case, target=target)
    assert feedback.status == "FAILED"
    assert feedback.error.code in {"NOT_FOUND", "REVIEW_TARGET_INVALID"}
    assert not trace(client, research_case)["evidence"]


def test_real_multi_image_exact_character_costume_scene_prop_and_model_bounds(
    client, episode, settings, research_case, research_storage
):
    refs = [
        add_image(client, create_asset(client, episode["project_id"], kind), color=color)
        for kind, color in [
            ("CHARACTER", "blue"),
            ("CHARACTER", "red"),
            ("SCENE", "green"),
            ("PROP", "yellow"),
        ]
    ]
    case = generated_references(client, episode, research_case, refs)
    index = query(client, settings, case, include_images=False)
    assert index.status == "SUCCEEDED", index.model_dump_json()
    assert set(index.result["reference_version_ids"]) == {ref["id"] for ref in refs}
    assert index.result["image_file_ids"] == []
    result = query(client, settings, case, reference_version_ids=[ref["id"] for ref in refs])
    assert result.status == "SUCCEEDED", result.model_dump_json()
    assert not any(item["reason"] == "NOT_REQUESTED" for item in result.result["missing"])
    file_ids = result.result["image_file_ids"]
    loaded = images(client, settings, case, file_ids)
    assert len(loaded) == 5 and len({image.data for image in loaded.values()}) == 5
    image_materials = {
        item["content"]["image"]["file_id"]: item
        for item in result.result["materials"]
        if item["source_kind"] in {"CANDIDATE", "ASSET"} and item["content"]["image"]
    }
    assert set(image_materials) == set(file_ids)
    for identity, image in loaded.items():
        material = image_materials[str(identity)]
        reference = material["content"]["image"]
        assert reference["asset_version_id"] == material["content"]["asset"]["asset_version_id"]
        assert reference["checksum"] == hashlib.sha256(image.data).hexdigest()
    stored = trace(client, case)["evidence"][-1]
    assert all(source["checksum"] and source["metadata_sha256"] for source in stored["sources"])
    assert base64.b64encode(next(iter(loaded.values())).data).decode() not in str(stored)
    declaration = AgentModelCapabilities(
        version="1",
        supplier="mock",
        model="mock",
        tools=True,
        response_formats=["json_schema"],
        max_input_images=2,
    )
    with pytest.raises(DomainError, match="图片数量"):
        images(client, settings, case, file_ids, capabilities=declaration)
    with pytest.raises(DomainError) as exc:
        images(client, settings, case, file_ids, through_round=0)
    assert exc.value.code == "NOT_FOUND"


def test_unbound_newer_reference_version_is_not_substituted(
    client, episode, settings, research_case
):
    asset = create_asset(client, episode["project_id"])
    old = add_image(client, asset, color="blue")
    case = generated_references(client, episode, research_case, [old])
    newer = add_image(client, asset, color="red")
    refused = query(client, settings, case, reference_version_ids=[newer["id"]])
    assert refused.status == "FAILED" and refused.error.code == "REVIEW_REFERENCE_INVALID"
    assert not trace(client, case)["evidence"]
    exact = query(client, settings, case, reference_version_ids=[old["id"]])
    assert exact.status == "SUCCEEDED", exact.model_dump_json()
    assert {
        item["source_id"] for item in exact.result["materials"] if item["source_kind"] == "ASSET"
    } == {old["id"]}


@pytest.mark.parametrize("location", ["candidate", "reference"])
def test_corrupt_images_fail_collection_and_consumption_without_partial_evidence(
    client, episode, settings, research_case, location
):
    ref = add_image(client, create_asset(client, episode["project_id"]))
    case = generated_references(client, episode, research_case, [ref])
    first = query(client, settings, case, reference_version_ids=[ref["id"]])
    assert first.status == "SUCCEEDED", first.model_dump_json()
    version_id = case["target"]["asset_version_id"] if location == "candidate" else ref["id"]
    object_path(
        settings.asset_storage_path, UUID(episode["project_id"]), UUID(version_id)
    ).write_bytes(b"broken image")
    failed = query(client, settings, case, reference_version_ids=[ref["id"]])
    assert failed.status == "FAILED" and failed.error.code == "ASSET_OBJECT_UNAVAILABLE"
    assert len(trace(client, case)["evidence"]) == 1
    with pytest.raises(DomainError):
        images(client, settings, case, first.result["image_file_ids"])


def test_freezes_profiles_replays_and_refresh_only_appends(
    client, episode, settings, research_case
):
    asset = create_asset(client, episode["project_id"])
    ref = add_image(client, asset)
    case = generated_references(client, episode, research_case, [ref])
    first = query(client, settings, case, reference_version_ids=[ref["id"]], key="frozen")
    assert first.status == "SUCCEEDED", first.model_dump_json()
    historical = trace(client, case)
    with client.app.state.session_factory.begin() as db:
        row = db.get(VisualAsset, UUID(asset["id"]))
        row.character_profile = {"fixed_features": ["silver hair"], "images": []}
        row.revision += 1
    assert query(client, settings, case, reference_version_ids=[ref["id"]], key="frozen") == first
    second = query(client, settings, case, reference_version_ids=[ref["id"]])
    assert second.status == "SUCCEEDED" and second.result["reused_binding"]
    old = next(item for item in first.result["materials"] if item["source_kind"] == "ASSET")
    reused = next(item for item in second.result["materials"] if item["source_kind"] == "ASSET")
    assert reused["content"] == old["content"] and reused["bound_manifest_id"] == old["manifest_id"]
    refreshed = query(client, settings, case, reference_version_ids=[ref["id"]], refresh=True)
    assert refreshed.status == "SUCCEEDED", refreshed.model_dump_json()
    current = next(item for item in refreshed.result["materials"] if item["source_kind"] == "ASSET")
    assert current["content"]["asset"]["profile"]["fixed_features"] == ["silver hair"]
    assert current["bound_manifest_id"] == current["manifest_id"]
    assert trace(client, case, through=1) == historical
    changed = query(client, settings, case, key="frozen", include_images=False)
    assert changed.status == "FAILED" and changed.error.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("foreign_project", [False, True])
def test_cross_project_and_episode_target_are_refused(
    client, episode, settings, research_case, foreign_project
):
    project_id = episode["project_id"]
    if foreign_project:
        response = client.post("/api/v1/projects", json={"name": "Other project"})
        assert response.status_code == 201, response.text
        project_id = response.json()["id"]
    response = client.post(
        f"/api/v1/projects/{project_id}/episodes",
        json={"episode_no": 2, "title": "Another", "target_duration_ms": 30000},
    )
    assert response.status_code == 201, response.text
    another = response.json()

    source = save_script(client, another)
    board, shots = approved_board(client, another, source)
    target = deepcopy(research_case["target"])
    target.update(storyboard_id=board["id"], shot_id=shots[0]["id"])
    result = query(client, settings, research_case, target=target)
    assert result.status == "FAILED" and result.error.code == "REVIEW_TARGET_INVALID"
    if foreign_project:
        foreign_candidate = generated_references(client, another, research_case, [])
        result = query(client, settings, foreign_candidate)
        assert result.status == "FAILED" and result.error.code == "NOT_FOUND"
    assert not trace(client, research_case)["evidence"]


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://example.com/image.png"},
        {"include_images": 1},
        {"refresh": "true"},
        {"reference_version_ids": [str(uuid4())] * 2},
        {"reference_version_ids": [str(uuid4()) for _ in range(9)]},
    ],
)
def test_contract_rejects_unbounded_or_uncontrolled_arguments(research_case, changes):
    with pytest.raises(ValidationError):
        InvestigateContinuityParameters.model_validate(
            {"target": research_case["target"], **changes}
        )


def test_wrong_agent_and_archived_project_never_gather(client, settings, research_case):
    refused = query(client, settings, research_case, agent="director")
    assert refused.status == "FAILED"
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(trace(client, research_case)["task_context"]["project_id"]))
        project.archived = True
    refused = query(client, settings, research_case)
    assert refused.status == "FAILED"


@pytest.mark.parametrize("status", ["STALE", "REJECTED", "ARCHIVED"])
def test_current_candidate_lifecycle_is_rechecked_for_collection_and_images(
    client, settings, research_case, status
):
    first = query(client, settings, research_case)
    assert first.status == "SUCCEEDED"
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(research_case["target"]["asset_version_id"]))
        if status == "STALE":
            version.stale = True
        elif status == "REJECTED":
            version.status = status
        else:
            db.get(VisualAsset, version.asset_id).status = status
    failed = query(client, settings, research_case)
    assert failed.status == "FAILED" and failed.error.code == "REVIEW_TARGET_INVALID"
    with pytest.raises(DomainError):
        images(client, settings, research_case, first.result["image_file_ids"])
    assert len(trace(client, research_case)["evidence"]) == 1


@pytest.mark.parametrize("research_case", [False], indirect=True)
@pytest.mark.parametrize("change", ["viewer", "removed", "grant"])
def test_current_authority_prevents_replay_after_revocation(
    client, episode, settings, research_case, change
):
    account = client.post(
        "/api/v1/auth/register",
        json={
            "email": uuid4().hex + "@example.com",
            "password": " a very good password ",
            "display_name": "Reviewer investigator",
        },
    )
    assert account.status_code == 201, account.text
    user = account.json()["user"]
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/members",
        json={"email": user["email"], "role": "editor"},
    )
    assert response.status_code == 201, response.text
    with client.app.state.session_factory.begin() as db:
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=research_case["run_id"],
                agent_name="continuity_reviewer",
                allowed_tools=["investigate_continuity"],
            ),
            user["id"],
            build_agent_tool_registry(settings=settings),
        )
    first = query(client, settings, research_case, key="revoked-evidence")
    assert first.status == "SUCCEEDED", first.model_dump_json()
    with client.app.state.session_factory.begin() as db:
        if change == "grant":
            grant_agent_tools(
                db,
                AgentToolGrantRequest(
                    run_id=research_case["run_id"],
                    agent_name="continuity_reviewer",
                    allowed_tools=[],
                ),
                user["id"],
                build_agent_tool_registry(settings=settings),
            )
        else:
            member = db.scalar(
                select(ProjectMember).where(
                    ProjectMember.project_id == UUID(episode["project_id"]),
                    ProjectMember.user_id == UUID(user["id"]),
                )
            )
            if change == "viewer":
                member.role = "viewer"
            else:
                db.delete(member)
    refused = query(client, settings, research_case, key="revoked-evidence")
    assert refused.status == "FAILED"
    assert refused.error.code in {"USER_NOT_AUTHORIZED", "TOOL_NOT_AUTHORIZED"}
    assert len(trace(client, research_case)["evidence"]) == 1


@pytest.mark.parametrize("research_case", ["unstarted"], indirect=True)
def test_exact_costume_scene_prop_and_adjacent_selected_version_are_preserved(
    client, episode, settings, research_case
):
    with client.app.state.session_factory.begin() as db:
        refs = {
            kind: make_asset(db, episode["project_id"], kind)
            for kind in ("CHARACTER", "COSTUME", "SCENE", "PROP", "IMAGE")
        }
        original = db.get(Storyboard, UUID(research_case["target"]["storyboard_id"]))
        content = deepcopy(original.content_json)
        base_id, expected = str(original.id), original.version_no
    first = content["shots"][0]
    name = first["characters"][0]
    first["character_details"] = [
        {
            "character": name,
            "asset": refs["CHARACTER"],
            "costume": refs["COSTUME"],
            "action": "hold the red umbrella",
        }
    ]
    first["scene_asset"] = refs["SCENE"]
    first["props"] = [refs["PROP"]]
    content["shots"][1]["selected_visual_asset"] = refs["IMAGE"]
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards",
        json={
            "expected_version_no": expected,
            "base_version_id": base_id,
            "script_version_id": research_case["source"]["id"],
            "content": content,
        },
    )
    assert response.status_code == 201, response.text
    board = response.json()
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards/{board['id']}/review",
        json={"decision": "APPROVE"},
    )
    assert response.status_code == 200, response.text
    shots = client.get(
        f"/api/v1/episodes/{episode['id']}/shots", params={"storyboard_id": board["id"]}
    ).json()
    case = deepcopy(research_case)
    case["target"].update(storyboard_id=board["id"], shot_id=shots[0]["id"])
    case = start_case(client, episode, settings, case)
    feedback = query(client, settings, case)
    assert feedback.status == "SUCCEEDED", feedback.model_dump_json()
    assert set(feedback.result["reference_version_ids"]) == {
        ref["asset_version_id"] for ref in refs.values()
    }
    loaded = query(
        client,
        settings,
        case,
        reference_version_ids=[ref["asset_version_id"] for ref in refs.values()],
    )
    assert loaded.status == "SUCCEEDED", loaded.model_dump_json()
    shot_materials = [item for item in loaded.result["materials"] if item["source_kind"] == "SHOT"]
    assert (
        shot_materials[0]["content"]["shot"]["character_details"][0]["costume"] == refs["COSTUME"]
    )
    assert shot_materials[1]["content"]["shot"]["selected_visual_asset"] == refs["IMAGE"]
    missing = {
        item["selector"].get("asset_version_id")
        for item in loaded.result["missing"]
        if item["reason"] == "NO_IMAGE"
    }
    assert missing == {ref["asset_version_id"] for ref in refs.values()}
    assert len(loaded.result["image_file_ids"]) == 1


def test_visual_constraints_stay_at_the_original_run_version_even_on_refresh(
    client, episode, settings, research_case
):
    first = query(client, settings, research_case)
    old = next(item for item in first.result["materials"] if item["source_kind"] == "VISUAL")
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(episode["project_id"]))
        project.style_guide = {"palette": "red", "locked": ["palette"]}
    refreshed = query(client, settings, research_case, refresh=True)
    assert refreshed.status == "SUCCEEDED", refreshed.model_dump_json()
    new = next(item for item in refreshed.result["materials"] if item["source_kind"] == "VISUAL")
    assert new["content"] == old["content"]
    assert new["bound_manifest_id"] == first.result["startup_manifest_id"]


def test_large_required_evidence_rolls_back_instead_of_trimming(
    client, episode, settings, research_case
):
    refs = []
    for _ in range(4):
        asset = create_asset(client, episode["project_id"], "SCENE")
        ref = add_image(client, asset)
        with client.app.state.session_factory.begin() as db:
            db.get(VisualAsset, UUID(asset["id"])).scene_profile = {
                "layout": "large layout " * 2000,
                "lighting": "rain " * 1000,
            }
        refs.append(ref)
    case = generated_references(client, episode, research_case, refs)
    before = trace(client, case)
    result = query(client, settings, case, reference_version_ids=[ref["id"] for ref in refs])
    assert result.status == "FAILED" and result.error.code == "CONTEXT_TOO_LARGE"
    assert trace(client, case) == before


def test_wrong_active_step_cannot_collect_even_with_a_grant(
    client, episode, settings, research_case
):
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, research_case["run_id"])
        run.agent_state_json = transition_agent_step(
            run.agent_state_json,
            scope_for_run(run, UUID(episode["project_id"])),
            expected_revision=run.agent_state_json["revision"],
            step_id="storyboard",
            target="SUCCEEDED",
            next_step_id="storyboard_review",
            next_step_status="RUNNING",
        ).model_dump(mode="json")
    feedback = query(client, settings, research_case)
    assert feedback.status == "FAILED" and feedback.error.code == "AGENT_NOT_ALLOWED"
    assert not trace(client, research_case)["evidence"]


@pytest.mark.parametrize("same_key", [True, False])
def test_postgres_concurrent_investigations_share_the_original_round_gate(
    client, settings, research_case, same_key
):
    require_postgres(settings)
    barrier = Barrier(2)

    def collect(index):
        barrier.wait(timeout=10)
        return query(
            client, settings, research_case, key="duplicate" if same_key else f"round-{index}"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(collect, range(2)))
    assert all(result.status == "SUCCEEDED" for result in results), [
        r.model_dump_json() for r in results
    ]
    if same_key:
        assert results[0] == results[1]
    saved = trace(client, research_case)["evidence"]
    assert len(saved) == (1 if same_key else 2)
    assert [row["round"] for row in saved] == list(range(1, len(saved) + 1))


@pytest.mark.parametrize("legacy_check", [None, {}, {"status": "FAILED"}])
def test_missing_or_failed_media_checks_cannot_enter_evidence(
    client, episode, settings, research_case, legacy_check
):
    from arclight.persistence.generation_candidates import GenerationCandidate

    with client.app.state.session_factory.begin() as db:
        original = db.get(GenerationCandidate, UUID(research_case["target"]["candidate_id"]))
        legacy = GenerationCandidate(
            project_id=original.project_id,
            job_id=original.job_id,
            attempt_id=original.attempt_id,
            input_manifest_id=original.input_manifest_id,
            ordinal=100,
            source="GENERATED",
            output_handle="historical-fixture",
            creation_key=uuid4(),
            request_fingerprint="a" * 64,
            error_code="MEDIA_CHECK_FAILED",
            media_check_json=legacy_check,
            created_by=research_case["actor"],
        )
        db.add(legacy)
        db.flush()
        identity = str(legacy.id)
    target = deepcopy(research_case["target"])
    target["candidate_id"] = identity
    result = query(client, settings, research_case, target=target)
    assert result.status == "FAILED" and result.error.code == "MEDIA_CHECK_REQUIRED"
    assert not trace(client, research_case)["evidence"]


def test_candidate_stays_evidence_even_if_a_fixture_already_approved_its_version(
    client, settings, research_case
):
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(research_case["target"]["asset_version_id"])).status = "APPROVED"
    result = query(client, settings, research_case)
    assert result.status == "SUCCEEDED", result.model_dump_json()
    evidence = trace(client, research_case)["evidence"][0]
    source_index = next(
        index
        for index, source in enumerate(evidence["sources"])
        if source.get("asset_version_id") == research_case["target"]["asset_version_id"]
    )
    memory = next(
        item for item in evidence["memory_sources"] if item["source_index"] == source_index
    )
    assert memory["category"] == "EVIDENCE"
    assert memory.get("confirmation_rule") is None


def test_image_count_and_bytes_fail_explicitly_without_a_partial_round(
    client, episode, settings, research_case
):
    refs = [add_image(client, create_asset(client, episode["project_id"])) for _ in range(8)]
    case = generated_references(client, episode, research_case, refs)
    result = query(client, settings, case, reference_version_ids=[ref["id"] for ref in refs])
    assert result.status == "FAILED" and result.error.code == "GATEWAY_CAPABILITY_REJECTED"
    assert not trace(client, case)["evidence"]
    metadata = query(
        client,
        settings,
        case,
        reference_version_ids=[ref["id"] for ref in refs],
        include_images=False,
    )
    assert metadata.status == "SUCCEEDED", metadata.model_dump_json()
    target_only = query(client, settings, case)
    assert target_only.status == "SUCCEEDED"
    capability = AgentModelCapabilities(
        version="1",
        supplier="mock",
        model="mock",
        tools=True,
        response_formats=["json_schema"],
        max_input_images=8,
        max_image_bytes=1,
    )
    with pytest.raises(DomainError) as exc:
        images(
            client, settings, case, target_only.result["image_file_ids"], capabilities=capability
        )
    assert exc.value.code == "GATEWAY_CAPABILITY_REJECTED"


def test_generic_retrieval_never_grants_the_candidate_image_exception(
    client, settings, research_case
):
    response = client.post(
        f"/api/v1/runs/{research_case['run_id']}/contexts/storyboard/evidence",
        json={
            "creation_key": str(uuid4()),
            "expected_round": 0,
            "asset_version_ids": [research_case["target"]["asset_version_id"]],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONTEXT_INPUT_NOT_USABLE"
    assert not trace(client, research_case)["evidence"]


@pytest.mark.parametrize("research_case", ["unstarted"], indirect=True)
def test_generation_content_binding_cannot_be_replaced_by_another_board_version(
    client, episode, settings, research_case
):
    case = generated_references(
        client,
        episode,
        research_case,
        [{"id": research_case["target"]["storyboard_id"]}],
        dependency_type="CONTENT_DEPENDENCY",
    )
    with client.app.state.session_factory() as db:
        original = db.get(Storyboard, UUID(case["target"]["storyboard_id"]))
        content = deepcopy(original.content_json)
        expected = original.version_no
    content["shots"][0]["notes"] = "Different exact version"
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards",
        json={
            "expected_version_no": expected,
            "base_version_id": case["target"]["storyboard_id"],
            "script_version_id": case["source"]["id"],
            "content": content,
        },
    )
    assert response.status_code == 201, response.text
    board = response.json()
    approved = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards/{board['id']}/review",
        json={"decision": "APPROVE"},
    )
    assert approved.status_code == 200, approved.text
    shots = client.get(
        f"/api/v1/episodes/{episode['id']}/shots", params={"storyboard_id": board["id"]}
    ).json()
    case["target"].update(storyboard_id=board["id"], shot_id=shots[0]["id"])
    case = start_case(client, episode, settings, case)
    result = query(client, settings, case)
    assert result.status == "FAILED" and result.error.code == "REVIEW_TARGET_INVALID"
    assert not trace(client, case)["evidence"]


def test_missing_permission_binding_requires_refresh_and_refresh_remains_effective(
    client, episode, settings, research_case
):
    asset = create_asset(client, episode["project_id"])
    ref = add_image(client, asset)
    case = generated_references(client, episode, research_case, [ref])
    with client.app.state.session_factory.begin() as db:
        row = db.get(VisualAsset, UUID(asset["id"]))
        row.metadata_json = {"usage_scope": {"kind": "EPISODES", "episode_ids": [str(uuid4())]}}
    missing = query(client, settings, case, reference_version_ids=[ref["id"]])
    assert missing.status == "SUCCEEDED", missing.model_dump_json()
    assert {
        "selector": {"asset_version_id": ref["id"]},
        "reason": "OUT_OF_SCOPE",
    } in missing.result["missing"]
    assert not any(item["source_kind"] == "ASSET" for item in missing.result["materials"])
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(asset["id"])).metadata_json = {"usage_scope": {"kind": "PROJECT"}}
    frozen = query(client, settings, case, reference_version_ids=[ref["id"]])
    assert frozen.status == "SUCCEEDED" and frozen.result["missing"] == missing.result["missing"]
    fresh = query(client, settings, case, reference_version_ids=[ref["id"]], refresh=True)
    assert fresh.status == "SUCCEEDED", fresh.model_dump_json()
    assert any(item["source_id"] == ref["id"] for item in fresh.result["materials"])
    later = query(client, settings, case, reference_version_ids=[ref["id"]])
    assert later.status == "SUCCEEDED"
    assert any(item["source_id"] == ref["id"] for item in later.result["materials"])
    assert not any(item["reason"] == "OUT_OF_SCOPE" for item in later.result["missing"])


@pytest.mark.parametrize("kind", ["IMAGE", "CHARACTER"])
def test_unchecked_managed_image_and_art_candidates_cannot_masquerade_as_shot_evidence(
    client, episode, settings, research_case, kind
):
    case = deepcopy(research_case)
    if kind == "CHARACTER":
        case = generated_references(client, episode, case, [], target_type=kind)
    else:
        from test_asset_versions import save as save_image

        with client.app.state.session_factory.begin() as db:
            asset = VisualAsset(
                project_id=UUID(episode["project_id"]),
                asset_type="IMAGE",
                name="Manual generic image",
                metadata_json={},
            )
            db.add(asset)
            db.flush()
            asset_id = str(asset.id)
        asset = client.get(f"/api/v1/projects/{episode['project_id']}/assets/{asset_id}").json()
        version = save_image(client, asset)
        case["target"].update(asset_id=asset_id, asset_version_id=version["id"])
        with client.app.state.session_factory() as db:
            from arclight.application.generation_candidates import require_candidate_review

            row = db.get(AssetVersion, UUID(version["id"]))
            with pytest.raises(DomainError) as exc:
                require_candidate_review(row, db)
            assert exc.value.code == "MEDIA_CHECK_REQUIRED"
    result = query(client, settings, case)
    assert result.status == "FAILED" and result.error.code == "REVIEW_TARGET_INVALID"
    assert not trace(client, case)["evidence"]


def test_local_review_reuses_exact_script_character_bindings_and_names_missing_roles(
    client, episode, settings, research_case
):
    from test_text_contract_v2 import legacy_script

    asset = create_asset(client, episode["project_id"])
    ref = add_image(client, asset)
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "Exact character references", "target_duration_ms": 30000},
    )
    assert response.status_code == 201, response.text
    other = response.json()
    reference = {"asset_id": asset["id"], "asset_version_id": ref["id"]}
    content = legacy_script()
    content["character_assets"] = [{"character": "主角", "asset": reference}]
    source = save_script(client, other, content)
    board, shots = approved_board(client, other, source)
    case = deepcopy(research_case)
    case["target"].update(storyboard_id=board["id"], shot_id=shots[0]["id"])
    case["source"] = source
    case = start_case(client, other, settings, case)
    result = query(client, settings, case, reference_version_ids=[ref["id"]])
    assert result.status == "SUCCEEDED", result.model_dump_json()
    script = next(item for item in result.result["materials"] if item["source_kind"] == "SCRIPT")
    assert script["content"]["character_assets"] == [{"character": "主角", "asset": reference}]
    assert ref["id"] in result.result["reference_version_ids"]
    missing_roles = {item["selector"].get("role") for item in result.result["missing"]}
    assert "CHARACTER" not in missing_roles
    assert {"COSTUME", "SCENE", "ADJACENT_IMAGE"} <= missing_roles
    assert len(images(client, settings, case, result.result["image_file_ids"])) == 2
