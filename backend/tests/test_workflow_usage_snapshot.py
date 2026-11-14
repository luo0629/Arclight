"""A worker may admit a call while the workspace reads its run contract."""

from uuid import UUID

from sqlalchemy import event
from test_api_workflow import start
from test_model_gateway import request, route

from arclight.application.model_gateway import admit
from arclight.domain.model_gateway import GatewayPolicy


def test_run_usage_count_and_rows_share_a_snapshot_during_worker_admission(client, episode):
    run_id = UUID(start(client, episode).json()["run_id"])
    factory = client.app.state.session_factory
    selected = route(maximum_cost=None)
    incoming = request(episode, selected, task="script", workflow_id=run_id)
    admitted = []

    def before_bounded_read(connection, cursor, statement, parameters, context, executemany):
        if admitted or "model_gateway_calls" not in statement or "LIMIT" not in statement:
            return
        # Commit at a deterministic interleaving, without sleeps or weakening the contract.
        admitted.append(True)
        with factory.begin() as writer:
            admit(writer, incoming, selected, GatewayPolicy())

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", before_bounded_read)
    try:
        response = client.get(f"/api/v1/runs/{run_id}")
    finally:
        event.remove(engine, "before_cursor_execute", before_bounded_read)
    assert admitted == [True]
    assert response.status_code == 200, response.text
    usage = response.json()["contract"]["usage"]
    assert usage["total_calls"] == len(usage["calls"]) == 1
    assert usage["calls"][0]["status"] == "RESULT_UNKNOWN"
    assert usage["calls"][0]["usage"]["actual_cost"] is None
