from datetime import timedelta
from uuid import UUID, uuid4

from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.base import utcnow
from arclight.persistence.models import OutboxEvent, WorkflowRun
from arclight.workers.tasks import pending_workflow_events


def test_running_short_drama_specialists_republish_only_their_current_delivery(client, episode):
    factory = client.app.state.session_factory
    project_id, episode_id = UUID(episode["project_id"]), UUID(episode["id"])
    stamp = utcnow()
    old_delivery = stamp - timedelta(minutes=1)

    def run(kind, **inputs):
        return WorkflowRun(
            id=uuid4(),
            episode_id=episode_id,
            workflow_type=kind,
            status="RUNNING",
            revision=1,
            trace_id=uuid4().hex,
            idempotency_key=uuid4().hex,
            request_fingerprint="0" * 64,
            input_json=inputs,
        )

    root = run("producer", short_drama={"script_version_id": str(uuid4())})
    manual_root = run("producer")
    art, board = run("art_proposal"), run("storyboard")
    manual_art, unrelated = run("art_proposal"), run("script_storyboard")
    with factory.begin() as db:
        db.add_all([root, manual_root, art, board, manual_art, unrelated])
        db.flush()
        for child, parent in [
            (art, root),
            (board, root),
            (manual_art, manual_root),
            (unrelated, root),
        ]:
            db.add(
                AgentRunLimit(
                    run_id=child.id,
                    project_id=project_id,
                    root_run_id=parent.id,
                    parent_run_id=parent.id,
                    actor_id="scheduler-test",
                    input_sha256="0" * 64,
                    depth=1,
                    limits_json={},
                    deadline_at=stamp + timedelta(hours=1),
                )
            )
            db.add(OutboxEvent(run_id=child.id, revision=1, published_at=old_delivery))
        db.add(OutboxEvent(run_id=art.id, revision=0, published_at=old_delivery))
        db.flush()
        assert {(event.run_id, event.revision) for event in pending_workflow_events(db)} == {
            (art.id, 1),
            (board.id, 1),
        }

        loop = AgentLoop(
            run_id=art.id,
            project_id=project_id,
            actor_id="scheduler-test",
            operation_sha256="0" * 64,
            request_sha256="0" * 64,
            input_sha256="0" * 64,
            result_schema_sha256="0" * 64,
            thread_id=f"agent-loop:{uuid4()}",
            seed_json={},
            lease_token=uuid4(),
            lease_expires_at=stamp + timedelta(minutes=2),
        )
        db.add(loop)
        for event in pending_workflow_events(db):
            if event.run_id == board.id:
                event.published_at = stamp
        db.flush()
        assert not pending_workflow_events(db)

        loop.lease_expires_at = stamp - timedelta(seconds=1)
        db.flush()
        assert [(event.run_id, event.revision) for event in pending_workflow_events(db)] == [
            (art.id, 1)
        ]

        for status in ("WAITING_EXTERNAL", "WAITING_HUMAN"):
            loop.status = status
            db.flush()
            assert not pending_workflow_events(db)

        loop.status = "RUNNING"
        root.status = "CANCELLED"
        db.flush()
        assert not pending_workflow_events(db)
