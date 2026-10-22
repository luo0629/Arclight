"""TXT-04：真实存储下任务与事件的 offset、稳定排序和资源隔离契约。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from arclight.persistence.models import WorkflowEvent, WorkflowRun


def test_run_and_event_pagination(client, episode):
    episode_id = UUID(episode["id"])
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    run_ids = [uuid4() for _ in range(105)]
    event_ids = [uuid4() for _ in range(200)]
    with client.app.state.session_factory() as db:
        db.add_all(
            WorkflowRun(
                id=run_id,
                episode_id=episode_id,
                status="SUCCEEDED",
                trace_id=str(run_id),
                idempotency_key=str(run_id),
                request_fingerprint="pagination-fixture",
                input_json={},
                created_at=timestamp,
            )
            for run_id in run_ids
        )
        db.flush()
        db.add_all(
            WorkflowEvent(
                id=event_id,
                run_id=run_ids[0],
                event_type="pagination_fixture",
                payload_json={},
                created_at=timestamp,
            )
            for event_id in event_ids
        )
        db.add(
            WorkflowEvent(
                run_id=run_ids[1], event_type="other_run", payload_json={}, created_at=timestamp
            )
        )
        db.commit()

    def page(path, offset, limit=100):
        response = client.get(f"/api/v1{path}?limit={limit}&offset={offset}")
        assert response.status_code == 200, response.text
        return response.json()

    path = f"/episodes/{episode_id}/runs"
    first, second = page(path, 0), page(path, 100)
    assert len(first) == 100
    assert len(second) == 5
    assert [row["id"] for row in first + second] == sorted(map(str, run_ids))
    assert page(path, 105) == []
    assert page(path, 100, 20) == second
    assert page(path, 0) == first

    path = f"/runs/{run_ids[0]}/events"
    first, second = page(path, 0), page(path, 100)
    assert len(first) == len(second) == 100
    assert [row["id"] for row in first + second] == sorted(map(str, event_ids))
    assert page(path, 200) == []
    assert page(path, 100, 50) == second[:50]
    assert page(f"/runs/{run_ids[2]}/events", 0) == []
    assert client.get(f"/api/v1{path}?offset=-1").status_code == 422
    assert client.get(f"/api/v1{path}?limit=101").status_code == 422
