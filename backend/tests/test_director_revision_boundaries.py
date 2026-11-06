"""Real check feedback, exact source binding, local edit boundaries and compatibility."""

import copy
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_director_creation import approved_script, start
from test_director_revision import checks, install_revision
from test_screenwriter_creation import run_once
from test_text_contract_v2 import make_asset

from arclight.agents.contracts import ShotContent
from arclight.api.schemas import RunCreate, StoryboardRunCreate
from arclight.application.production_versions import text_definitions
from arclight.domain.director_revision import DirectorWorkingShot
from arclight.persistence.models import Review, Script, Storyboard


def missing(result, seed):
    if seed["scene"]["scene_no"] == 1:
        result["shots"][0]["dialogue_line_ids"] = []


@pytest.mark.parametrize(
    "fault",
    ["hash", "operation", "foreign_scene", "cross_scene", "range", "whole_scene", "too_wide"],
)
def test_invalid_scope_is_observed_then_corrected_without_leaking_changes(
    client, episode, settings, model_service, monkeypatch, fault
):
    def invalid(arguments, seed):
        patch = arguments["patch"]
        if fault == "hash":
            arguments["source_sha256"] = "f" * 64
        elif fault == "operation":
            arguments["operation_key"] = "other-operation"
        elif fault == "foreign_scene":
            patch["scene_id"] = str(uuid4())
        elif fault == "cross_scene":
            patch["shots"][0]["scene_id"] = seed["source"]["scenes"][1]["scene_id"]
        elif fault == "range":
            patch["start"], patch["end"] = 4, 2
        elif fault == "whole_scene":
            patch["shots"] = []
        else:
            patch["end"] = 21

    sent, errors, base_errors = install_revision(
        model_service, monkeypatch, settings, mutate=missing, invalid_first=invalid
    )
    run_id, _ = start(client, episode, director_revision_rounds=1)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, base_errors)
    first = checks(client, run_id)[0]
    feedback = [item["feedback"] for item in first["observations"]]
    assert (
        feedback[1]["status"] == "FAILED"
        if fault == "operation"
        else feedback[1]["result"]["accepted"] is False
    )
    assert feedback[-1]["result"]["accepted"] is True
    assert first["director_revision"]["patches"][-1]["selected"]
    assert any("DIRECTOR_" in str(item["messages"]) for item in sent)
    before = first["director_revision"]["working_storyboard"]["scenes"]
    after = checks(client, run_id)[-1]["director_revision"]["working_storyboard"]["scenes"]
    assert before[1:] == after[1:]


@pytest.mark.parametrize("kind", ["insert", "delete", "split"])
def test_insert_delete_split_renumber_only_outside_selected_range(
    client, episode, settings, model_service, monkeypatch, kind
):
    def mutate(result, seed):
        if seed["scene"]["scene_no"] != 1:
            return
        if kind == "insert":
            result["shots"].pop(0)
        elif kind == "delete":
            extra = copy.deepcopy(result["shots"][1])
            extra["duration_ms"] = 500
            result["shots"].insert(1, extra)
        else:
            result["shots"][0]["dialogue_line_ids"] = []

    def patch(seed, proposed):
        proposed["start"] = 0 if kind != "delete" else 1
        proposed["end"] = 0 if kind == "insert" else 2 if kind == "delete" else 1
        shots = [copy.deepcopy(proposed["shots"][0])] if kind != "delete" else []
        if kind == "split":
            original = shots[0]["duration_ms"]
            shots[0]["duration_ms"] = original // 2
            shots.append(copy.deepcopy(shots[0]))
            shots[1]["duration_ms"] = original - shots[0]["duration_ms"]
            shots[1]["dialogue_line_ids"] = []
            shots[1]["visual_description"] += "镜头切至纸船"
        return proposed | {"shots": shots}

    _, errors, base_errors = install_revision(
        model_service, monkeypatch, settings, mutate=mutate, make_patch=patch
    )
    run_id, _ = start(client, episode, director_revision_rounds=1)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, base_errors)
    before, after = [
        item["director_revision"]["working_storyboard"] for item in checks(client, run_id)
    ]

    def strip(shot):
        return {key: value for key, value in shot.items() if key != "shot_no"}

    for origin, changed in zip(before["scenes"][1:], after["scenes"][1:], strict=True):
        assert [strip(shot) for shot in origin["shots"]] == [
            strip(shot) for shot in changed["shots"]
        ]
    patch_value = checks(client, run_id)[0]["director_revision"]["patches"][-1]["patch"]
    assert [strip(shot) for shot in before["scenes"][0]["shots"][patch_value["end"] :]] == [
        strip(shot)
        for shot in after["scenes"][0]["shots"][patch_value["start"] + len(patch_value["shots"]) :]
    ]


@pytest.mark.parametrize("fault", ["source", "receipt", "accept_patch", "blind"])
def test_forged_or_wrong_decision_cannot_publish(
    client, episode, settings, model_service, monkeypatch, fault
):
    def report(decision, seed):
        result = decision.get("result")
        if not result:
            return
        if fault == "source":
            result["source_sha256"] = "f" * 64
        elif fault == "receipt":
            result["check_sha256"] = "f" * 64
        elif fault == "accept_patch":
            result["decision"] = "ACCEPT"

    _, errors, base_errors = install_revision(
        model_service,
        monkeypatch,
        settings,
        mutate=missing if fault == "accept_patch" else None,
        mutate_report=report,
        skip_check=fault == "blind",
    )
    run_id, _ = start(client, episode, director_revision_rounds=1)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED", (run, errors, base_errors)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.scalar(select(func.count()).select_from(Review)) == 0


def test_dialogue_order_and_truncated_issue_count_remain_blocking(
    client, episode, settings, model_service, monkeypatch
):
    def configure(content):
        scene = content["scenes"][0]
        scene["dialogue"] = [
            {"line_id": str(uuid4()), "speaker": "主角", "text": f"第{i}句"} for i in range(70)
        ]

    def mutate(result, seed):
        if seed["scene"]["scene_no"] == 1:
            for shot in result["shots"]:
                shot["dialogue_line_ids"] = []
            result["shots"][0]["dialogue_line_ids"] = [
                seed["scene"]["dialogue"][1]["line_id"],
                seed["scene"]["dialogue"][0]["line_id"],
            ]

    _, errors, base_errors = install_revision(model_service, monkeypatch, settings, mutate=mutate)
    source = approved_script(client, episode, configure=configure)
    run_id, _ = start(client, episode, source, director_revision_rounds=1)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, base_errors)
    checked = checks(client, run_id)[0]["observations"][0]["feedback"]["result"]
    assert (
        checked["issue_count"] == 69
        and checked["issues_truncated"]
        and len(checked["issues"]) == 48
    )
    assert "DIRECTOR_DIALOGUE_ORDER" in {issue["code"] for issue in checked["issues"]}
    assert not checked["valid"]


def test_strict_publication_contract_and_old_version_definitions_are_preserved():
    shot = {
        "shot_no": -1,
        "scene_no": 0,
        "duration_ms": 1,
        "visual_description": "工作稿",
        "dialogue_line_ids": [str(UUID(int=1))] * 2,
    }
    work = DirectorWorkingShot.model_validate(shot)
    with pytest.raises(ValidationError):
        ShotContent.model_validate(work.model_dump(mode="json"))
    for version in ("director/1.0", "production/1.0", "production/2.0"):
        definitions = {kind: value for kind, _, value in text_definitions("storyboard", version)}
        assert definitions["AGENT"]["fragment_version"] == "director-fragment/1"
        assert definitions["AGENT"]["assembly"] == "complete-scenes-contiguous-shots/1"
        assert "revision" not in definitions["PROMPT"] and "revision" not in definitions["SCHEMA"]
    assert (
        "revision"
        not in {kind: value for kind, _, value in text_definitions("script", "production/3.0")}[
            "PROMPT"
        ]
    )
    assert (
        "revision"
        in {kind: value for kind, _, value in text_definitions("script", "production/4.0")}[
            "PROMPT"
        ]
    )


def test_entry_validation_and_idempotency_include_director_round_limit(
    client, episode, settings, model_service, monkeypatch
):
    for value in (-1, 9, True, 1.5, "1"):
        with pytest.raises(ValidationError):
            StoryboardRunCreate(
                script_version_id=uuid4(), professional=True, director_revision_rounds=value
            )
    with pytest.raises(ValidationError):
        StoryboardRunCreate(script_version_id=uuid4(), director_revision_rounds=1)
    with pytest.raises(ValidationError):
        RunCreate(story="故事", professional=True, director_revision_rounds=1)
    install_revision(model_service, monkeypatch, settings)
    source = approved_script(client, episode)
    body = {"script_version_id": source["id"], "professional": True, "director_revision_rounds": 1}
    url = f"/api/v1/episodes/{episode['id']}/storyboard-runs"
    headers = {"Idempotency-Key": uuid4().hex}
    first = client.post(url, headers=headers, json=body)
    assert first.status_code == 202
    assert client.post(url, headers=headers, json=body).json() == first.json()
    assert (
        client.post(url, headers=headers, json=body | {"director_revision_rounds": 2}).status_code
        == 409
    )


def test_repair_keeps_script_and_approved_exact_bindings(
    client, episode, settings, model_service, monkeypatch
):
    with client.app.state.session_factory.begin() as db:
        refs = {
            kind: make_asset(db, episode["project_id"], kind)
            for kind in ("CHARACTER", "SCENE", "PROP")
        }

    def configure(content):
        content["character_assets"] = [{"character": "主角", "asset": refs["CHARACTER"]}]
        for scene in content["scenes"]:
            scene["scene_asset"] = refs["SCENE"]
            scene["props"] = [refs["PROP"]]

    _, errors, base_errors = install_revision(model_service, monkeypatch, settings, mutate=missing)
    source = approved_script(client, episode, configure=configure)
    run_id, _ = start(client, episode, source, director_revision_rounds=1)
    assert run_once(client, settings, run_id)["status"] == "WAITING_REVIEW", (errors, base_errors)
    with client.app.state.session_factory() as db:
        board = db.scalar(select(Storyboard))
        for shot in board.content_json["shots"]:
            assert shot["character_details"][0]["asset"] == refs["CHARACTER"]
            assert shot["scene_asset"] == refs["SCENE"] and shot["props"] == [refs["PROP"]]
        assert db.get(Script, UUID(source["id"])).content_json == source["content_json"]
