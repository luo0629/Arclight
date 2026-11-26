"""Exact creative decisions and API helpers for render preflight integration tests."""

from uuid import UUID, uuid4

from render_manifest_helpers import fixture_actor, manifest
from test_timeline_saves import saved_timeline

from arclight.persistence.models import Script, Storyboard


def edited(client, timeline, snapshot):
    return saved_timeline(
        client,
        timeline,
        {
            "creation_key": str(uuid4()),
            "expected_version_no": timeline["version_no"],
            "snapshot": snapshot,
        },
    )


def line_decisions(client, timeline):
    voiced = {
        (clip["script_version_id"], clip["line_id"])
        for track in timeline["snapshot"]["tracks"]
        if track["kind"] == "VOICE" and track.get("enabled", True)
        for clip in track["clips"]
    }
    with client.app.state.session_factory() as db:
        board = db.get(Storyboard, UUID(timeline["snapshot"]["storyboard_id"]))
        script = db.get(Script, board.script_version_id)
        return [
            {
                "script_version_id": str(script.id),
                "line_id": line["line_id"],
                "mode": "SILENT",
                "reason": "本次静态预演由创作者明确选择静默呈现",
            }
            for scene in script.content_json["scenes"]
            for line in scene["dialogue"]
            if (str(script.id), line["line_id"]) not in voiced
        ]


def ready_manifest(client, timeline, **overrides):
    return manifest(
        client, timeline, line_audio_decisions=line_decisions(client, timeline), **overrides
    )


def render_path(value, suffix):
    return f"/api/v1/render-manifests/{value['id']}/{suffix}"


def request_export(client, value):
    """Seed an exact review record for tests of downstream services."""
    from arclight.api.human_reviews import serialize
    from arclight.application.human_reviews import request_review
    from arclight.application.render_admission import admission_facts
    from arclight.application.render_manifests import manifest_row
    from arclight.application.render_reviews import EXPORT_HANDLER, export_snapshot

    with client.app.state.session_factory.begin() as db:
        actor = fixture_actor(client, db)
        row = manifest_row(db, UUID(value["id"]), actor, write=True)
        snapshot = export_snapshot(row, admission_facts(db, row))
        review = request_review(db, actor, snapshot.thread_id, EXPORT_HANDLER, snapshot)
        return serialize(db, review).model_dump(mode="json")


def record_export_decision(client, row, decision="APPROVE"):
    """Seed the durable decision independently when testing a later stage."""
    from test_human_reviews import decision_payload

    from arclight.api.human_reviews import DecisionRead
    from arclight.application.human_reviews import submit_decision
    from arclight.domain.human_reviews import HumanDecision

    payload = HumanDecision.model_validate(decision_payload(row, decision))
    with client.app.state.session_factory.begin() as db:
        saved = submit_decision(db, UUID(row["id"]), fixture_actor(client, db), payload)
        return DecisionRead.model_validate(saved).model_dump(mode="json")


def record_export_resume(client, row, decision):
    """Seed a resume receipt without starting a job."""
    from arclight.api.human_reviews import ResumeRead
    from arclight.application.human_reviews import resume_review
    from arclight.domain.human_reviews import ReviewResume

    payload = ReviewResume(decision_id=UUID(decision["id"]), request_sha256=row["request_sha256"])
    with client.app.state.session_factory.begin() as db:
        receipt = resume_review(db, UUID(row["id"]), fixture_actor(client, db), payload)
        return ResumeRead.model_validate(receipt).model_dump(mode="json")


def approve_export_fixture(client, value):
    review = request_export(client, value)
    decision = record_export_decision(client, review)
    return record_export_resume(client, review, decision)


def start_formal_export(client, timeline, **overrides):
    return client.post(
        f"/api/v1/timelines/{timeline['id']}/formal-export",
        json={
            "creation_key": str(uuid4()),
            "line_audio_decisions": line_decisions(client, timeline),
            **overrides,
        },
    )


def formal_export_command(client, value, action, **overrides):
    return client.post(render_path(value, "formal-export"), json={"action": action, **overrides})


def assert_blocked(response, code):
    assert response.status_code in {403, 404, 409, 422}, response.text
    assert response.json()["error"]["code"] == code, response.text
