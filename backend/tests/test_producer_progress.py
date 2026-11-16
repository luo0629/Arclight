"""Public progress follows the same gate-before-root order as command execution."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_concurrency import require_postgres
from test_producer_planning import completed_plan
from test_producer_replanning import install_replan_model, observation, replanned

from arclight.application import producer_progress
from arclight.application.model_gateway import lock_gate
from arclight.persistence.models import WorkflowRun


def test_postgres_progress_waits_for_gate_without_holding_root(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    run_id, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    observed = observation(client, settings, run_id, actor, reference)
    install_replan_model(model_service)
    _, current = replanned(client, settings, run_id, actor, observed)
    factory, entering_gate = client.app.state.session_factory, Event()

    def reader_gate(db):
        entering_gate.set()
        lock_gate(db)

    def read():
        with factory.begin() as db:
            return producer_progress.read_progress(db, run_id, actor, settings)

    monkeypatch.setattr(producer_progress, "lock_gate", reader_gate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as db:
            lock_gate(db)
            future = pool.submit(read)
            assert entering_gate.wait(timeout=20), "Progress never reached its admission gate"
            # A reader awaiting the gate must not prevent a writer from locking the root.
            root = db.scalar(
                select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update(nowait=True)
            )
            assert root is not None
        value = future.result(timeout=30)
    assert value["plans"][-1]["plan_id"] == str(current.plan_id)
    assert value["plans"][-1]["version_no"] == 2
    assert value["plans"][-1]["tasks"][0]["can_delegate"]
