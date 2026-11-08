"""QA-03 checks real tool calls against fixed images and annotated comparisons."""

import base64
import hashlib
import json
import os
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image, ImageChops
from pydantic import ValidationError
from sqlalchemy import func, select
from storage_helpers import clear_test_bucket
from test_asset_selection import choose
from test_asset_versions import save, state
from test_assets import create as create_asset
from test_contexts import approved_board
from test_continuity_research import images, query
from test_director_planning import prepare
from test_generation_candidates import candidates, ingest
from test_generation_delivery import deliver
from test_generation_jobs import capture_ok, payload
from test_text_contract_v2 import legacy_script, make_asset, save_script

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import (
    build_agent_tool_registry,
    execute_agent_tool,
    grant_agent_tools,
)
from arclight.application.asset_objects import object_key, object_path, write_object
from arclight.application.continuity_rules import check_continuity_rules
from arclight.application.files import record_asset_file
from arclight.core.errors import DomainError
from arclight.domain.agent_state import transition_agent_step
from arclight.domain.agent_tools import AgentToolCall, AgentToolGrantRequest
from arclight.domain.continuity_rules import ContinuityRulesParameters
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AgentToolGrant,
    AssetSelectionLock,
    AssetVersion,
    GenerationAttempt,
    Project,
    ProjectMember,
    Shot,
    VersionBinding,
    VisualAsset,
    WorkflowRun,
)
from arclight.providers.media_outputs import MediaOutput
from arclight.providers.media_submission import MockMediaSubmissionProvider
from arclight.storage.objects import ObjectStorage, initialize

FIXTURES = Path(__file__).parent / "fixtures" / "continuity_rules"
ANNOTATIONS = json.loads((FIXTURES / "annotations.json").read_text(encoding="utf-8"))
RULES = {
    "APPEARANCE",
    "COSTUME",
    "SCENE",
    "PROP",
    "INTENT",
    "COMPOSITION",
    "HOLDER",
    "POSITION",
    "STATE",
    "ADJACENT",
}


@pytest.fixture
def rules_storage(request, settings):
    storage = getattr(request, "param", "local")
    if storage == "s3":
        endpoint = os.getenv("ARCLIGHT_TEST_S3_ENDPOINT")
        if not endpoint:
            pytest.skip("Requires isolated S3 test service")
        settings.object_storage_backend = "s3"
        settings.s3_endpoint_url = endpoint
        settings.s3_access_key = type(settings.s3_access_key)("arclight-test")
        settings.s3_secret_key = type(settings.s3_secret_key)("arclight-test-secret")
        settings.s3_bucket = "arclight-test-qa03-" + uuid4().hex
        initialize(settings)
    try:
        yield storage
    finally:
        if storage == "s3":
            clear_test_bucket(ObjectStorage(settings))


class FixedImageProvider:
    name = "mock"

    def __init__(self, filename):
        self.data = (FIXTURES / filename).read_bytes()

    def download(self, request, handle, max_bytes):
        assert handle == f"mock/image/{request.attempt_id}/0"
        assert len(self.data) <= max_bytes
        return MediaOutput(self.data, "image/png")


def reference_image(client, project_id, kind, name, filename):
    asset = create_asset(client, project_id, kind, name=name)
    version = save(
        client, asset, content_base64=base64.b64encode((FIXTURES / filename).read_bytes()).decode()
    )
    ready = state(client, asset, version, "READY_FOR_REVIEW")
    assert ready.status_code == 200, ready.text
    approved = state(client, asset, ready.json(), "APPROVED")
    assert approved.status_code == 200, approved.text
    return {"asset_id": asset["id"], "asset_version_id": version["id"]}


def imported_reference(client, settings, project_id, kind, filename):
    """Seed an approved historical reference using the actual controlled object services."""
    data = (FIXTURES / filename).read_bytes()
    identity = uuid4()
    project_id = UUID(project_id)
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
    with client.app.state.session_factory.begin() as db:
        asset = VisualAsset(project_id=project_id, asset_type=kind, name=kind, status="APPROVED")
        db.add(asset)
        db.flush()
        version = AssetVersion(
            id=identity,
            asset_id=asset.id,
            version_no=1,
            status="APPROVED",
            created_by="service-owner",
            checksum=hashlib.sha256(data).hexdigest(),
            object_key=object_key(project_id, identity, settings),
            metadata_json={
                "storage_backend": settings.object_storage_backend.upper(),
                "media": {
                    "width": width,
                    "height": height,
                    "byte_size": len(data),
                    "media_type": "image/png",
                    "original_name": filename,
                    "source_note": "Fixed historical reference fixture",
                    "rights_note": "Self-created schematic fixture",
                    "rights_confirmed": True,
                },
            },
        )
        write_object(db, settings, project_id, identity, data, "image/png")
        db.add(version)
        db.flush()
        record_asset_file(db, settings, project_id, version)
        return {"asset_id": str(asset.id), "asset_version_id": str(version.id)}


@pytest.fixture
def rules_case(client, episode, settings, rules_storage, request):
    scenario = getattr(request, "param", "match")
    refs = {
        role: reference_image(client, episode["project_id"], kind, role, filename)
        for role, kind, filename in [
            ("CHARACTER", "CHARACTER", "character.png"),
            ("SCENE", "SCENE", "scene-reference.png"),
            ("PROP", "PROP", "prop-reference.png"),
            ("SECOND_CHARACTER", "CHARACTER", "second-character.png"),
        ]
    }
    refs["COSTUME_IMAGE"] = imported_reference(
        client, settings, episode["project_id"], "COSTUME", "costume-reference.png"
    )
    refs["NEIGHBOR_IMAGE"] = imported_reference(
        client,
        settings,
        episode["project_id"],
        "IMAGE",
        "transition.png" if scenario == "transition" else "neighbor.png",
    )
    refs["COSTUME"] = refs["COSTUME_IMAGE"]
    refs["ADJACENT"] = refs["NEIGHBOR_IMAGE"]
    if scenario == "directory_character":
        refs["CHARACTER_IMAGE"] = refs["CHARACTER"]
        with client.app.state.session_factory.begin() as db:
            refs["CHARACTER"] = make_asset(db, episode["project_id"], "CHARACTER")
            asset = db.get(VisualAsset, UUID(refs["CHARACTER"]["asset_id"]))
            asset.character_profile = {
                "standard_image_id": refs["CHARACTER_IMAGE"]["asset_version_id"],
                "images": [
                    {"id": refs["CHARACTER_IMAGE"]["asset_version_id"], **refs["CHARACTER_IMAGE"]}
                ],
                "unrelated_reference": refs["SCENE"],
            }
    script_content = legacy_script()
    script_content["characters"].append("同行")
    if scenario == "script_character":
        script_content["character_assets"] = [{"character": "主角", "asset": refs["CHARACTER"]}]
    script_content["scenes"][0]["action"] = ANNOTATIONS["baseline"]
    if scenario == "transition":
        second = deepcopy(script_content["scenes"][0])
        second.update(scene_no=2, title="另一房间", action=ANNOTATIONS["transition"])
        script_content["scenes"].append(second)
    script = save_script(client, episode, content=script_content)
    board, _ = approved_board(client, episode, script)
    content = deepcopy(board["content_json"])
    content["shots"] = content["shots"][:2]
    first, neighbor = content["shots"]
    first.update(
        characters=["主角"],
        character_details=[
            {
                "character": "主角",
                "asset": refs["CHARACTER"],
                "costume": refs["COSTUME"],
                "action": "hold the closed red case",
            }
        ],
        scene_asset=refs["SCENE"],
        props=[refs["PROP"]],
        composition="Full shot, protagonist on the left looking right; show the red case.",
        visual_description=ANNOTATIONS["baseline"],
    )
    neighbor["selected_visual_asset"] = refs["ADJACENT"]
    if scenario == "two_characters":
        first["characters"].append("同行")
        first["character_details"].append(
            {"character": "同行", "asset": refs["SECOND_CHARACTER"], "costume": refs["COSTUME"]}
        )
    if scenario == "missing_bindings":
        first["character_details"] = []
        first["scene_asset"] = None
        neighbor["selected_visual_asset"] = None
    if scenario == "empty":
        first.update(
            characters=[],
            character_details=[],
            props=[],
            dialogue_line_ids=[],
            visual_description="Empty blue room without people or cases.",
            composition="Full shot of the empty room.",
        )
        content["shots"] = [first]
    if scenario == "transition":
        neighbor.update(
            scene_no=2,
            scene_id=None,
            dialogue_line_ids=[],
            visual_description=ANNOTATIONS["transition"],
        )
    for shot in content["shots"]:
        shot["duration_ms"] = 30000 // len(content["shots"])
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards",
        json={
            "expected_version_no": board["version_no"],
            "base_version_id": board["id"],
            "script_version_id": script["id"],
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
    body = payload()
    body["spec"]["output_spec"] = {"width": 64, "height": 96, "format": "png"}
    job = capture_ok(client, episode["project_id"], body)
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    filename = scenario + ".png" if scenario + ".png" in ANNOTATIONS["sha256"] else "match.png"
    if scenario == "transition":
        filename = "match.png"
    elif scenario == "empty":
        filename = "scene-reference.png"
    assert ingest(client, attempt, FixedImageProvider(filename)) == 1
    candidate = candidates(client, episode["project_id"], job)[0]
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(candidate["asset_version_id"]))
        target = {
            "candidate_id": candidate["id"],
            "asset_id": str(version.asset_id),
            "asset_version_id": str(version.id),
            "input_manifest_id": candidate["input_manifest_id"],
            "media_check_sha256": digest(candidate["media_check"]),
            "storyboard_id": board["id"],
            "shot_id": shots[0]["id"],
        }
    run_id, actor, _ = prepare(client, episode, settings, source={"id": script["id"]})
    if scenario == "member_actor":
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": uuid4().hex + "@example.com",
                "password": " a very good password ",
                "display_name": "Rule reviewer",
            },
        )
        assert response.status_code == 201, response.text
        user = response.json()["user"]
        response = client.post(
            f"/api/v1/projects/{episode['project_id']}/members",
            json={"email": user["email"], "role": "editor"},
        )
        assert response.status_code == 201, response.text
        actor = user["id"]
    case = {"target": target, "source": {"id": script["id"]}, "run_id": run_id, "actor": actor}
    with client.app.state.session_factory.begin() as db:
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=case["run_id"],
                agent_name="continuity_reviewer",
                allowed_tools=["investigate_continuity", "check_continuity_rules"],
            ),
            case["actor"],
            build_agent_tool_registry(settings=settings),
        )
    index = query(client, settings, case, include_images=False)
    assert index.status == "SUCCEEDED", index.model_dump_json()
    result = query(
        client, settings, case, reference_version_ids=index.result["reference_version_ids"]
    )
    assert result.status == "SUCCEEDED", result.model_dump_json()
    if scenario == "directory_character":
        result = query(
            client, settings, case, reference_version_ids=result.result["reference_version_ids"]
        )
        assert result.status == "SUCCEEDED", result.model_dump_json()
    return case | {
        "evidence": result.result,
        "refs": refs,
        "filename": filename,
        "project_id": episode["project_id"],
        "shots": shots,
    }


def check(
    client, settings, case, comparisons=None, *, key=None, agent="continuity_reviewer", **changes
):
    with client.app.state.session_factory.begin() as db:
        return execute_agent_tool(
            db,
            build_agent_tool_registry(settings=settings),
            agent,
            AgentToolCall(
                run_id=case["run_id"],
                tool_name="check_continuity_rules",
                idempotency_key=key or uuid4().hex,
                arguments={
                    "target": case["target"],
                    "evidence_manifest_id": case["evidence"]["manifest_id"],
                    "comparisons": comparisons or [],
                    **changes,
                },
            ),
        )


def checked(feedback):
    assert feedback.status == "SUCCEEDED", feedback.model_dump_json()
    return feedback.result


def proposals(plan):
    common = [
        s
        for s in plan["context_sources"]
        if s["source_kind"] in {"CANDIDATE", "VISUAL", "SCRIPT"}
        or s["source_kind"] == "SHOT"
        and s["source_id"] == plan["target"]["shot_id"]
    ]
    result = []
    for item in plan["checks"]:
        sources = {(s["source_kind"], s["source_id"]): s for s in common}
        for ref in item["references"]:
            for source in ref["image_sources"] + (
                [ref["adjacent_shot"]] if ref["adjacent_shot"] else []
            ):
                sources[source["source_kind"], source["source_id"]] = source
        result.append(
            {
                "rule_id": item["definition"]["rule_id"],
                "status": "MATCH" if item["applicable"] else "NOT_APPLICABLE",
                "summary": (
                    "Compare the fixed annotated candidate with the exact frozen requirements."
                ),
                "citations": [
                    {k: v for k, v in source.items() if k != "image_file_id"}
                    | {"basis": "Fixed image and exact frozen context comparison."}
                    for source in sources.values()
                ],
            }
        )
    return result


def by_rule(result):
    return {row["definition"]["rule_id"]: row for row in result["checks"]}


def test_fixture_annotations_have_exact_bytes_and_visible_local_differences():
    for name, checksum in ANNOTATIONS["sha256"].items():
        data = (FIXTURES / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == checksum
        with Image.open(BytesIO(data)) as image:
            image.verify()
    baseline = Image.open(FIXTURES / "match.png").convert("RGB")
    assert {item["rule_id"] for item in ANNOTATIONS["differences"]} == RULES
    for annotation in ANNOTATIONS["differences"]:
        region = annotation["region"]
        box = (
            int(region["x"] * 64),
            int(region["y"] * 96),
            int((region["x"] + region["width"]) * 64),
            int((region["y"] + region["height"]) * 96),
        )
        image = Image.open(FIXTURES / annotation["file"]).convert("RGB")
        assert ImageChops.difference(baseline.crop(box), image.crop(box)).getbbox()


@pytest.mark.parametrize("rules_storage", ["local", "s3"], indirect=True)
def test_fixed_matches_cover_exact_roles_and_never_admit_or_select(client, settings, rules_case):
    case = rules_case
    plan = checked(check(client, settings, case))
    rules = by_rule(plan)
    assert set(rules) == RULES and all(item["applicable"] for item in rules.values())
    assert plan["rules_version"] == "continuity-rules/1"
    assert not plan["complete"] and plan["suggested_conclusion"] is None
    expected = {
        "APPEARANCE": "CHARACTER",
        "COSTUME": "COSTUME_IMAGE",
        "SCENE": "SCENE",
        "PROP": "PROP",
        "ADJACENT": "NEIGHBOR_IMAGE",
    }
    for rule, role in expected.items():
        refs = rules[rule]["references"]
        assert len(refs) == 1
        assert [s["source_id"] for s in refs[0]["image_sources"]] == [
            case["refs"][role]["asset_version_id"]
        ], (rule, refs, case["evidence"])
    loaded = images(client, settings, case, case["evidence"]["image_file_ids"])
    candidate = next(m for m in case["evidence"]["materials"] if m["source_kind"] == "CANDIDATE")
    assert (
        loaded[UUID(candidate["content"]["image"]["file_id"])].data
        == (FIXTURES / "match.png").read_bytes()
    )
    with client.app.state.session_factory() as db:
        before = [
            db.scalar(select(func.count()).select_from(model))
            for model in (VersionBinding, AssetSelectionLock)
        ]
    result = checked(check(client, settings, case, proposals(plan), key="fixed-match"))
    assert (
        result["complete"] and result["suggested_conclusion"] == "PASS" and result["advisory_only"]
    )
    assert all(row["status"] == "MATCH" and not row["gaps"] for row in result["checks"])
    with client.app.state.session_factory() as db:
        assert before == [
            db.scalar(select(func.count()).select_from(model))
            for model in (VersionBinding, AssetSelectionLock)
        ]
        version = db.get(AssetVersion, UUID(case["target"]["asset_version_id"]))
        asset = db.get(VisualAsset, version.asset_id)
        assert version.status == "DRAFT" and asset.current_version_id is None
    root = f"/api/v1/projects/{case['project_id']}/assets/{case['target']['asset_id']}"
    asset = client.get(root).json()
    version = client.get(root + f"/versions/{case['target']['asset_version_id']}").json()
    assert choose(client, asset, version).json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    assert checked(check(client, settings, case, proposals(plan), key="fixed-match")) == result


@pytest.mark.parametrize(
    "rules_case", [item["file"][:-4] for item in ANNOTATIONS["differences"]], indirect=True
)
def test_annotated_differences_route_by_risk_and_keep_exact_regions(client, settings, rules_case):
    case = rules_case
    annotation = next(
        item for item in ANNOTATIONS["differences"] if item["file"] == case["filename"]
    )
    plan = checked(check(client, settings, case))
    values = proposals(plan)
    comparison = next(item for item in values if item["rule_id"] == annotation["rule_id"])
    comparison.update(status="DIFFERENCE", risk=annotation["risk"], summary=annotation["summary"])
    citation = next(item for item in comparison["citations"] if item["source_kind"] == "CANDIDATE")
    citation.update(region=annotation["region"], basis=annotation["summary"])
    result = checked(check(client, settings, case, values))
    expected = "HUMAN_REVIEW" if annotation["risk"] in {"HIGH", "CRITICAL"} else "REVISION_REQUIRED"
    assert result["complete"] and result["suggested_conclusion"] == expected
    observed = by_rule(result)[annotation["rule_id"]]["comparison"]
    assert observed["summary"] == annotation["summary"] and observed["risk"] == annotation["risk"]
    assert next(c for c in observed["citations"] if c["source_kind"] == "CANDIDATE") == citation
    assert observed["uncertainty"] is None


@pytest.mark.parametrize("rules_case", ["occluded"], indirect=True)
@pytest.mark.parametrize(
    "reason", ["OCCLUDED", "MISSING_REFERENCE", "LOW_VISIBILITY", "CONTRADICTION"]
)
def test_uncertainty_never_becomes_a_complete_pass(client, settings, rules_case, reason):
    plan = checked(check(client, settings, rules_case))
    values = proposals(plan)
    values[1].update(
        status="UNDETERMINED",
        uncertainty={"reason": reason, "detail": ANNOTATIONS["occlusion"]["detail"]},
    )
    result = checked(check(client, settings, rules_case, values))
    assert not result["complete"] and result["suggested_conclusion"] == "HUMAN_REVIEW"


@pytest.mark.parametrize("rules_case", ["missing_bindings"], indirect=True)
def test_absent_bound_images_cannot_be_replaced_by_claiming_match(client, settings, rules_case):
    plan = checked(check(client, settings, rules_case))
    rules = by_rule(plan)
    for rule, role in [
        ("APPEARANCE", "CHARACTER"),
        ("COSTUME", "COSTUME"),
        ("SCENE", "SCENE"),
        ("ADJACENT", "ADJACENT"),
    ]:
        assert any(gap.startswith("MISSING_REFERENCE_IMAGE:" + role) for gap in rules[rule]["gaps"])
    result = checked(check(client, settings, rules_case, proposals(plan)))
    assert not result["complete"] and result["suggested_conclusion"] == "HUMAN_REVIEW"


@pytest.mark.parametrize(
    "change",
    ["candidate", "references", "omitted", "false_na", "script", "visual", "neighbor_description"],
)
def test_incomplete_comparisons_cannot_pass(client, settings, rules_case, change):
    case = rules_case
    if change in {"candidate", "references"}:
        evidence = query(client, settings, case, include_images=change != "candidate")
        case = case | {"evidence": checked(evidence)}
    plan = checked(check(client, settings, case))
    values = proposals(plan)
    if change == "omitted":
        values = values[:-1]
    elif change == "false_na":
        values[0].update(status="NOT_APPLICABLE", citations=[])
    elif change in {"script", "visual", "neighbor_description"}:
        kind = {"script": "SCRIPT", "visual": "VISUAL", "neighbor_description": "SHOT"}[change]
        values[-1]["citations"] = [
            item
            for item in values[-1]["citations"]
            if not (
                item["source_kind"] == kind
                and (kind != "SHOT" or item["source_id"] != case["target"]["shot_id"])
            )
        ]
    result = checked(check(client, settings, case, values))
    assert not result["complete"] and result["suggested_conclusion"] == "HUMAN_REVIEW"


@pytest.mark.parametrize(
    "rule,role",
    [
        ("APPEARANCE", "CHARACTER"),
        ("COSTUME", "COSTUME_IMAGE"),
        ("SCENE", "SCENE"),
        ("PROP", "PROP"),
        ("ADJACENT", "NEIGHBOR_IMAGE"),
    ],
)
def test_each_role_requires_its_own_reference_not_another_valid_image(
    client, settings, rules_case, rule, role
):
    plan = checked(check(client, settings, rules_case))
    values = proposals(plan)
    comparison = next(item for item in values if item["rule_id"] == rule)
    comparison["citations"] = [
        item
        for item in comparison["citations"]
        if item["source_id"] != rules_case["refs"][role]["asset_version_id"]
    ]
    # An unrelated but genuinely collected image must not substitute for the omitted role.
    other_role = "SCENE" if role != "SCENE" else "PROP"
    other = next(
        s
        for s in plan["context_sources"]
        if s["source_id"] == rules_case["refs"][other_role]["asset_version_id"]
    )
    comparison["citations"].append(
        {k: v for k, v in other.items() if k != "image_file_id"} | {"basis": "Wrong role"}
    )
    result = checked(check(client, settings, rules_case, values))
    assert not result["complete"] and by_rule(result)[rule]["status"] == "UNDETERMINED"


@pytest.mark.parametrize("rules_case", ["two_characters"], indirect=True)
def test_appearance_requires_every_character_not_just_one(client, settings, rules_case):
    plan = checked(check(client, settings, rules_case))
    assert {r["subject"] for r in by_rule(plan)["APPEARANCE"]["references"]} == {"主角", "同行"}
    values = proposals(plan)
    values[0]["citations"] = [
        c
        for c in values[0]["citations"]
        if c["source_id"] != rules_case["refs"]["SECOND_CHARACTER"]["asset_version_id"]
    ]
    result = checked(check(client, settings, rules_case, values))
    assert "UNCITED_REFERENCE:CHARACTER:同行" in by_rule(result)["APPEARANCE"]["gaps"]
    assert result["suggested_conclusion"] == "HUMAN_REVIEW"


@pytest.mark.parametrize("rules_case", ["empty"], indirect=True)
def test_only_actually_absent_subjects_can_be_not_applicable(client, settings, rules_case):
    plan = checked(check(client, settings, rules_case))
    absent = {rule for rule, row in by_rule(plan).items() if not row["applicable"]}
    assert absent == {"APPEARANCE", "COSTUME", "PROP", "HOLDER", "STATE", "ADJACENT"}
    values = proposals(plan)
    result = checked(check(client, settings, rules_case, values))
    assert result["complete"] and result["suggested_conclusion"] == "PASS"
    values[0].update(status="MATCH")
    feedback = check(client, settings, rules_case, values)
    assert feedback.error.code == "REVIEW_RULE_NOT_APPLICABLE"


@pytest.mark.parametrize("rules_case", ["transition"], indirect=True)
def test_explicit_scene_change_preserves_adjacent_evidence_without_same_scene_constraints(
    client, settings, rules_case
):
    plan = checked(check(client, settings, rules_case))
    assert plan["scene_change_shot_ids"] == [rules_case["shots"][1]["id"]]
    rules = by_rule(plan)
    assert (
        rules["ADJACENT"]["references"][0]["adjacent_shot"]["source_id"]
        == rules_case["shots"][1]["id"]
    )
    assert not rules["POSITION"]["references"]
    assert {r["role"] for r in rules["HOLDER"]["references"]} == {"PROP"}
    result = checked(check(client, settings, rules_case, proposals(plan)))
    assert result["complete"] and result["suggested_conclusion"] == "PASS"


@pytest.mark.parametrize(
    "change,code",
    [
        ("hash", "REVIEW_REFERENCE_CHANGED"),
        ("source", "REVIEW_REFERENCE_INVALID"),
        ("manifest", "REVIEW_REFERENCE_INVALID"),
        ("target", "REVIEW_TARGET_INVALID"),
        ("region_on_text", "REVIEW_EVIDENCE_INVALID"),
    ],
)
def test_forged_precise_sources_targets_and_text_regions_are_refused(
    client, settings, rules_case, change, code
):
    plan = checked(check(client, settings, rules_case))
    values = proposals(plan)
    args = {}
    if change == "hash":
        values[0]["citations"][0]["content_sha256"] = "0" * 64
    elif change == "source":
        values[0]["citations"][0]["source_id"] = str(uuid4())
    elif change == "manifest":
        args["evidence_manifest_id"] = str(uuid4())
    elif change == "target":
        args["target"] = rules_case["target"] | {"shot_id": str(uuid4())}
    else:
        citation = next(c for c in values[0]["citations"] if c["source_kind"] == "SCRIPT")
        citation["region"] = {"x": 0.0, "y": 0.0, "width": 0.2, "height": 0.2}
    result = check(client, settings, rules_case, values, **args)
    assert result.status == "FAILED" and result.error.code == code


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_rule",
        "unknown_rule",
        "too_many_rules",
        "empty_summary",
        "unknown_field",
        "outside_region",
        "riskless_difference",
        "uncertain_match",
        "unexplained_unknown",
        "no_citations",
        "too_many_citations",
        "bad_checksum",
    ],
)
def test_bounded_comparison_contract_rejects_invalid_input(client, settings, rules_case, change):
    values = proposals(checked(check(client, settings, rules_case)))
    first = values[0]
    if change == "duplicate_rule":
        values[-1]["rule_id"] = first["rule_id"]
    elif change == "unknown_rule":
        first["rule_id"] = "QUALITY_SCORE"
    elif change == "too_many_rules":
        values.append(deepcopy(first))
    elif change == "empty_summary":
        first["summary"] = " "
    elif change == "unknown_field":
        first["internal_reasoning"] = "must not be accepted"
    elif change == "outside_region":
        first["citations"][0]["region"] = {"x": 0.9, "y": 0.0, "width": 0.2, "height": 0.2}
    elif change == "riskless_difference":
        first["status"] = "DIFFERENCE"
    elif change == "uncertain_match":
        first["uncertainty"] = {"reason": "OCCLUDED", "detail": "hidden"}
    elif change == "unexplained_unknown":
        first["status"] = "UNDETERMINED"
    elif change == "no_citations":
        first["citations"] = []
    elif change == "too_many_citations":
        first["citations"] = [first["citations"][0]] * 17
    elif change == "bad_checksum":
        first["citations"][0]["content_sha256"] = "guess"
    result = check(client, settings, rules_case, values)
    assert result.status == "FAILED" and result.error.code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize(
    "change",
    [
        "stale_candidate",
        "rejected_candidate",
        "stale_reference",
        "corrupt_image",
        "stale_neighbor",
        "reference_scope",
    ],
)
def test_fresh_check_revalidates_current_inputs_while_replay_keeps_observation(
    client, settings, rules_case, change
):
    plan = checked(check(client, settings, rules_case))
    values = proposals(plan)
    first = checked(check(client, settings, rules_case, values, key="original-observation"))
    with client.app.state.session_factory.begin() as db:
        candidate = db.get(AssetVersion, UUID(rules_case["target"]["asset_version_id"]))
        if change == "stale_candidate":
            candidate.stale = True
        elif change == "rejected_candidate":
            candidate.status = "REJECTED"
        elif change == "stale_reference":
            db.get(
                AssetVersion, UUID(rules_case["refs"]["CHARACTER"]["asset_version_id"])
            ).stale = True
        elif change == "stale_neighbor":
            db.get(Shot, UUID(rules_case["shots"][1]["id"])).status = "STALE"
        elif change == "reference_scope":
            asset = db.get(VisualAsset, UUID(rules_case["refs"]["CHARACTER"]["asset_id"]))
            asset.metadata_json = {
                **asset.metadata_json,
                "usage_scope": {"kind": "EPISODES", "episode_ids": [str(uuid4())]},
            }
        else:
            object_path(
                settings.asset_storage_path, UUID(rules_case["project_id"]), candidate.id
            ).write_bytes(b"broken")
    assert checked(check(client, settings, rules_case, values, key="original-observation")) == first
    fresh = check(client, settings, rules_case, values)
    assert fresh.status == "FAILED" and fresh.error.code != "EXECUTION_FAILED"


def test_explicit_tool_grant_is_required_even_for_idempotent_replay(client, settings, rules_case):
    values = proposals(checked(check(client, settings, rules_case)))
    assert checked(check(client, settings, rules_case, values, key="revoked"))["complete"]
    with client.app.state.session_factory.begin() as db:
        grant = db.scalar(
            select(AgentToolGrant).where(
                AgentToolGrant.run_id == rules_case["run_id"],
                AgentToolGrant.agent_name == "continuity_reviewer",
            )
        )
        grant.allowed_tools_json = ["investigate_continuity"]
    result = check(client, settings, rules_case, values, key="revoked")
    assert result.status == "FAILED" and result.error.code == "USER_NOT_AUTHORIZED"


def test_changed_idempotent_request_and_other_agents_cannot_reuse_rule_result(
    client, settings, rules_case
):
    values = proposals(checked(check(client, settings, rules_case)))
    checked(check(client, settings, rules_case, values, key="immutable"))
    values[0]["summary"] = "a changed observation"
    assert (
        check(client, settings, rules_case, values, key="immutable").error.code
        == "IDEMPOTENCY_CONFLICT"
    )
    assert check(client, settings, rules_case, values, agent="director").status == "FAILED"


@pytest.mark.parametrize("rules_case", ["script_character", "directory_character"], indirect=True)
def test_reuses_script_bindings_and_only_the_directory_standard_image(client, settings, rules_case):
    plan = checked(check(client, settings, rules_case))
    appearance = by_rule(plan)["APPEARANCE"]["references"]
    expected = rules_case["refs"].get("CHARACTER_IMAGE", rules_case["refs"]["CHARACTER"])
    assert len(appearance) == 1 and appearance[0]["subject"] == "主角"
    assert [s["source_id"] for s in appearance[0]["image_sources"]] == [
        expected["asset_version_id"]
    ]
    assert (
        checked(check(client, settings, rules_case, proposals(plan)))["suggested_conclusion"]
        == "PASS"
    )


@pytest.mark.parametrize("rules_case", ["directory_character"], indirect=True)
@pytest.mark.parametrize("change", ["stale", "scope"])
def test_directory_reference_is_revalidated_even_when_its_standard_image_is_usable(
    client, settings, rules_case, change
):
    values = proposals(checked(check(client, settings, rules_case)))
    with client.app.state.session_factory.begin() as db:
        if change == "stale":
            db.get(
                AssetVersion, UUID(rules_case["refs"]["CHARACTER"]["asset_version_id"])
            ).stale = True
        else:
            asset = db.get(VisualAsset, UUID(rules_case["refs"]["CHARACTER"]["asset_id"]))
            asset.metadata_json = {
                **asset.metadata_json,
                "usage_scope": {"kind": "EPISODES", "episode_ids": [str(uuid4())]},
            }
    result = check(client, settings, rules_case, values)
    assert result.status == "FAILED" and result.error.code in {
        "CONTEXT_INPUT_NOT_USABLE",
        "CONTEXT_SCOPE_NOT_ALLOWED",
    }


@pytest.mark.parametrize("rules_case", ["member_actor"], indirect=True)
@pytest.mark.parametrize("change", ["viewer", "removed"])
def test_current_member_authority_is_checked_before_replay(client, settings, rules_case, change):
    values = proposals(checked(check(client, settings, rules_case)))
    checked(check(client, settings, rules_case, values, key="membership"))
    with client.app.state.session_factory.begin() as db:
        member = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == UUID(rules_case["project_id"]),
                ProjectMember.user_id == UUID(rules_case["actor"]),
            )
        )
        if change == "viewer":
            member.role = "viewer"
        else:
            db.delete(member)
    result = check(client, settings, rules_case, values, key="membership")
    assert result.status == "FAILED" and result.error.code in {
        "USER_NOT_AUTHORIZED",
        "TOOL_NOT_AUTHORIZED",
    }


def test_archived_project_refuses_new_checks_and_replays(client, settings, rules_case):
    values = proposals(checked(check(client, settings, rules_case)))
    checked(check(client, settings, rules_case, values, key="archived"))
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(rules_case["project_id"])).archived = True
    for key in ["archived", "fresh"]:
        assert check(client, settings, rules_case, values, key=key).status == "FAILED"


def test_current_step_must_still_allow_comparison(client, settings, rules_case):
    values = proposals(checked(check(client, settings, rules_case)))
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, rules_case["run_id"])
        run.agent_state_json = transition_agent_step(
            run.agent_state_json,
            scope_for_run(run, UUID(rules_case["project_id"])),
            expected_revision=run.agent_state_json["revision"],
            step_id="storyboard",
            target="SUCCEEDED",
            next_step_id="storyboard_review",
            next_step_status="RUNNING",
        ).model_dump(mode="json")
    result = check(client, settings, rules_case, values)
    assert result.status == "FAILED" and result.error.code == "AGENT_NOT_ALLOWED"


@pytest.mark.parametrize("foreign_project", [False, True])
def test_other_run_cannot_reuse_an_exact_frozen_manifest(
    client, episode, settings, rules_case, foreign_project
):
    project_id = episode["project_id"]
    if foreign_project:
        response = client.post("/api/v1/projects", json={"name": "Other comparison project"})
        assert response.status_code == 201, response.text
        project_id = response.json()["id"]
    response = client.post(
        f"/api/v1/projects/{project_id}/episodes",
        json={"episode_no": 2, "title": "Other comparison", "target_duration_ms": 30000},
    )
    assert response.status_code == 201, response.text
    other = response.json()
    script = save_script(client, other)
    run_id, actor, _ = prepare(client, other, settings, source=script)
    with client.app.state.session_factory.begin() as db:
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=run_id,
                agent_name="continuity_reviewer",
                allowed_tools=["check_continuity_rules"],
            ),
            actor,
            build_agent_tool_registry(settings=settings),
        )
    case = rules_case | {"run_id": run_id, "actor": actor}
    result = check(client, settings, case)
    assert result.status == "FAILED" and result.error.code == "REVIEW_REFERENCE_INVALID"


def test_critical_and_explicit_uncertainty_always_require_human_suggestion(
    client, settings, rules_case
):
    values = proposals(checked(check(client, settings, rules_case)))
    values[0].update(status="DIFFERENCE", risk="CRITICAL", summary="Conflicting identity")
    assert (
        checked(check(client, settings, rules_case, values))["suggested_conclusion"]
        == "HUMAN_REVIEW"
    )
    values[0].update(
        risk="LOW", uncertainty={"reason": "CONTRADICTION", "detail": "Unresolved sources"}
    )
    assert (
        checked(check(client, settings, rules_case, values))["suggested_conclusion"]
        == "HUMAN_REVIEW"
    )


def test_oversized_comparison_feedback_fails_explicitly(client, settings, rules_case):
    values = proposals(checked(check(client, settings, rules_case)))
    for value in values:
        value["summary"] = "界" * 500
        citation = deepcopy(value["citations"][0])
        citation["basis"] = "据" * 500
        value["citations"] = [citation] * 16
    arguments = {
        "target": rules_case["target"],
        "evidence_manifest_id": rules_case["evidence"]["manifest_id"],
        "comparisons": values,
    }
    with pytest.raises(ValidationError, match="size limit"):
        AgentToolCall(
            run_id=rules_case["run_id"],
            tool_name="check_continuity_rules",
            idempotency_key="oversized",
            arguments=arguments,
        )
    # The service also enforces its output limit if called without the executor boundary.
    parameters = ContinuityRulesParameters.model_validate(arguments)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, rules_case["run_id"])
        with pytest.raises(DomainError) as exc:
            check_continuity_rules(db, settings, run, rules_case["actor"], parameters)
        assert exc.value.code == "CONTEXT_TOO_LARGE"
