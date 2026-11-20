"""Exact recorded voice provenance is not a claim of hearing or a drift verdict."""

from uuid import uuid4

from test_audio_voice_profiles import (
    complete_recording,
    post,
    tts_case_for,
)
from test_audio_voice_profiles import (
    series_voice_case as series_voice_case,  # noqa: F401
)
from test_narrative_review import execute, progress, start
from test_series_planning import planned_outline as planned_outline  # noqa: F401
from test_writing_context import writing_case as writing_case  # noqa: F401


def test_approved_story_voice_change_is_frozen_with_line_time_and_reason(
    client, settings, series_voice_case
):
    case = series_voice_case
    profile = post(client, case["url"], case["payload"])
    source = case["sources"][0]
    recording, _, _, _ = complete_recording(
        client, settings, tts_case_for(source, 1, profile, case["changed"])
    )
    content = source["script"]["content_json"]
    context = content["narrative_context"]
    review_case = {"episode": source["episode"]}
    body = {
        "creation_key": str(uuid4()),
        "scope": "SCENE",
        "baseline": {
            "series_plan_version_id": context["series_plan_version_id"],
            "ledger_version_id": case["ledger"]["head"]["approved_version_id"],
            "expected_ledger_head_revision": case["ledger"]["head"]["revision"],
        },
        "targets": [
            {
                "script_version_id": source["script"]["id"],
                "scene_ids": [content["scenes"][1]["scene_id"]],
                "audio_version_ids": [recording["id"]],
            }
        ],
    }
    run_id = start(client, review_case, body)
    execute(client, settings, run_id)
    value = progress(client, review_case, run_id)
    assert value["status"] == "WAITING_REVIEW", value
    audio = next(item for item in value["work"]["evidence"] if item["kind"] == "AUDIO")
    selection = audio["value"]["voice_selection"]
    assert selection["voice_version_id"] == case["changed"]["id"]
    assert selection["story_time"] == 10
    assert selection["change_reason"] == "回忆中含着药，声音发哑"
    assert audio["value"]["voice_profile"]["id"] == profile["id"]
    assert audio["location"]["line_id"] == source["lines"][1]["line_id"]
    assert "未试听" in audio["value"]["observation"]
    assert "未试听或作声学评估" in str(value["report"]["limitations"])
