"""Exercise the shared Director with automatic dialogue and sound constraints."""

import json
from uuid import UUID, uuid4

from test_agent_model import model_service as model_service
from test_audio_production import wave_bytes
from test_contexts import approved_board
from test_director_creation import approved_script
from test_director_revision import checks, install_revision
from test_music_assets import approve_music, create_music, import_music, music_source, music_url
from test_screenwriter_creation import run_once
from test_short_drama_delivery import authorized_run

from arclight.application.commands import start_run
from arclight.application.screenwriter_creation import creation_loops
from arclight.persistence.models import WorkflowRun


def test_short_drama_director_repairs_unavailable_sound_without_removing_dialogue(
    client, episode, settings, model_service, monkeypatch
):
    script = approved_script(client, episode, count=1)
    board, _ = approved_board(client, episode, script)
    root_id, _ = authorized_run(client, episode, UUID(board["id"]), generated_rights=True)
    asset = create_music(client, episode)
    source = music_source(client, episode, data=wave_bytes())
    version = approve_music(client, asset, import_music(client, asset, source, "AMBIENCE"))
    asset = client.get(music_url(asset)).json()
    selected = client.post(
        music_url(asset, f"/versions/{version['id']}/select"),
        json={
            "expected_revision": asset["revision"],
            "expected_lock_version": asset["lock_version"],
        },
    )
    assert selected.status_code == 200, selected.text

    def add_sounds(result, _seed):
        result["shots"][0]["sound_effects"] = [asset["name"], "项目中没有的拟声音效"]

    def use_available_effect(_seed, patch):
        patch["shots"][0]["sound_effects"] = [asset["name"]]
        return patch

    sent, errors, plan_errors = install_revision(
        model_service, monkeypatch, settings, mutate=add_sounds, make_patch=use_available_effect
    )
    factory = client.app.state.session_factory
    with factory.begin() as db:
        child = start_run(
            db,
            UUID(episode["id"]),
            "service-owner",
            "",
            uuid4().hex,
            uuid4().hex,
            settings=settings,
            script_version_id=UUID(script["id"]),
            professional_director=True,
            director_revision_rounds=3,
            producer_parent_run_id=root_id,
        )
        child_id = child.id
    run = run_once(client, settings, child_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, plan_errors)
    reports = checks(client, child_id)
    assert [item["result"]["decision"] for item in reports] == ["REVISE", "ACCEPT"]
    feedback = reports[0]["observations"][0]["feedback"]["result"]
    assert {issue["code"] for issue in feedback["issues"]} == {"SHORT_DRAMA_SFX_REQUIRED"}
    assert reports[-1]["director_revision"]["initial_check"]["metrics"]["covered_lines"] == 1
    assert sent and all(
        json.loads(call["messages"][2]["content"])["data"]["sound_effects"] == [asset["name"]]
        for call in sent
    )
    with factory() as db:
        plan, *_ = creation_loops(db, child_id, "director")
        assert plan.seed_json["messages"][1]["data"]["sound_effects"] == [asset["name"]]
        assert db.get(WorkflowRun, root_id).status == "RUNNING"
