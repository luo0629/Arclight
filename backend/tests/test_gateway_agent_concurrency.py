"""Model preparation shares gateway lock order and releases business locks before I/O."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from time import monotonic
from uuid import UUID

import httpx
from sqlalchemy import event, select, text
from test_agent_model import FinalResult, active_run, final_response, model_request
from test_agent_model import model_service as model_service
from test_agent_tools import grant
from test_concurrency import require_postgres

from arclight.application import gateway_agent
from arclight.application.agent_tools import build_agent_tool_registry
from arclight.application.commands import require_project
from arclight.application.model_gateway import lock_gate
from arclight.persistence.models import Project, WorkflowRun


def unlocked_model_response(client, episode, run_id, prepared, completed):
    def respond(_request):
        assert prepared and all(not db.in_transaction() for db in prepared)
        with client.app.state.session_factory.begin() as db:
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SET LOCAL lock_timeout = '1500ms'"))
            lock_gate(db)
            project = db.scalar(
                select(Project)
                .where(Project.id == UUID(episode["project_id"]))
                .with_for_update(nowait=True)
            )
            run = db.scalar(
                select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update(nowait=True)
            )
            assert project is not None and run is not None
        completed.append(True)
        return httpx.Response(200, json=final_response())

    return respond


def test_model_preparation_acquires_gateway_gate_before_business_locks(
    client, episode, settings, model_service, monkeypatch
):
    run_id, actor = active_run(client, episode)
    registry = build_agent_tool_registry()
    grant(client, run_id, actor, registry)
    statements, prepared, completed = [], [], []
    original_tools = gateway_agent.available_agent_tools

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    def available(db, *args, **kwargs):
        assert statements[0].startswith("UPDATE model_gateway_gate "), (
            "Model preparation must acquire the shared gateway gate before business row locks"
        )
        prepared.append(db)
        return original_tools(db, *args, **kwargs)

    monkeypatch.setattr(gateway_agent, "available_agent_tools", available)
    model_service(unlocked_model_response(client, episode, run_id, prepared, completed))
    engine = client.app.state.engine
    event.listen(engine, "after_cursor_execute", record_sql)
    try:
        result = gateway_agent.invoke_agent(
            client.app.state.session_factory, settings, model_request(run_id), registry, FinalResult
        )
    finally:
        event.remove(engine, "after_cursor_execute", record_sql)
    assert result.final.summary == "已取证"
    assert completed == [True]


def test_postgres_model_preparation_waits_at_gate_without_locking_workflow(
    client, episode, settings, model_service, monkeypatch, record_property
):
    require_postgres(settings)
    run_id, actor = active_run(client, episode)
    registry = build_agent_tool_registry()
    grant(client, run_id, actor, registry)
    prepared, completed = [], []
    original_tools = gateway_agent.available_agent_tools
    started = Event()
    poll_delay = Event()
    worker = {}

    def available(db, *args, **kwargs):
        prepared.append(db)
        return original_tools(db, *args, **kwargs)

    def record_pid(connection, _cursor, _statement, _parameters, _context, _many):
        if worker.get("thread") == get_ident() and "pid" not in worker:
            worker["pid"] = connection.connection.driver_connection.info.backend_pid
            started.set()

    def competing():
        worker["thread"] = get_ident()
        return gateway_agent.invoke_agent(
            client.app.state.session_factory, settings, model_request(run_id), registry, FinalResult
        )

    monkeypatch.setattr(gateway_agent, "available_agent_tools", available)
    model_service(unlocked_model_response(client, episode, run_id, prepared, completed))
    engine, factory = client.app.state.engine, client.app.state.session_factory
    event.listen(engine, "before_cursor_execute", record_pid)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with factory.begin() as holder:
                lock_gate(holder)
                require_project(holder, UUID(episode["project_id"]), actor, write=True)
                holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
                future = pool.submit(competing)
                assert started.wait(timeout=10)
                deadline = monotonic() + 10
                blockers, waiting = [], None
                while monotonic() < deadline:
                    blockers = holder.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": worker["pid"]}
                    )
                    if holder_pid in blockers:
                        waiting = holder.execute(
                            text(
                                "SELECT wait_event_type, query FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": worker["pid"]},
                        ).one()
                        break
                    assert not future.done(), "Model preparation bypassed the held gateway gate"
                    poll_delay.wait(0.01)
                assert waiting is not None and waiting.wait_event_type == "Lock", blockers
                assert waiting.query.startswith("UPDATE model_gateway_gate "), waiting.query
                assert not prepared and not future.done()
                # The competing preparation must not hold the workflow while waiting.
                assert (
                    holder.scalar(
                        select(WorkflowRun)
                        .where(WorkflowRun.id == run_id)
                        .with_for_update(nowait=True)
                    )
                    is not None
                )
                record_property(
                    "model_preparation_gate_overlap",
                    repr({"holder": holder_pid, "worker": worker["pid"], "blockers": blockers}),
                )
            # The holder transaction releases every lock before joining the worker,
            # including when an assertion above raises and the context rolls back.
            result = future.result(timeout=20)
    finally:
        event.remove(engine, "before_cursor_execute", record_pid)
    assert result.final.summary == "已取证"
    assert completed == [True]
