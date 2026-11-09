from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_continuity_reviews import review_case
from test_timeline_save_concurrency import postgres_case
from test_timeline_saves import save_url, saved_case, saved_timeline
from test_timeline_visuals import configured_body
from test_timelines import timeline_case

from arclight.main import create_app
from arclight.persistence.models import AuditEvent
from arclight.persistence.timelines import Timeline, TimelineClipVisual, TimelineRevision

__all__ = ["postgres_case", "review_case", "saved_case", "timeline_case"]


def test_postgres_competing_visual_saves_publish_one_complete_frozen_configuration(
    client, postgres_case
):
    original, raw, _ = postgres_case
    first = configured_body(raw)
    second = deepcopy(first)
    second["creation_key"] = str(uuid4())
    second["snapshot"]["tracks"][0]["clips"][-1]["visual"]["crop"] = {"x": 1000, "width": 9000}
    barrier = Barrier(2)

    def save(body):
        with TestClient(create_app(client.app.state.settings)) as connection:
            connection.headers.update(dict(client.headers))
            barrier.wait(timeout=20)
            return connection.patch(save_url(original), json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, [first, second]))
    assert sorted(response.status_code for response in responses) == [201, 409], [
        r.text for r in responses
    ]
    winner = next(response.json() for response in responses if response.status_code == 201)
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["error"]["code"] == "VERSION_CONFLICT"
    assert winner["version_no"] == 2 and winner["snapshot"]["schema_version"] == 2
    assert client.get(save_url(original)).json() == original
    assert client.get(save_url(winner)).json() == winner
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(Timeline.id))) == 2
        assert db.scalar(select(func.count(TimelineRevision.id))) == 1
        assert db.scalar(select(func.count(TimelineClipVisual.id))) == 5
        assert set(db.scalars(select(TimelineClipVisual.timeline_id))) == {UUID(winner["id"])}
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == "timeline.saved")
            )
            == 1
        )


def test_postgres_truncate_cannot_remove_visual_edit_history(client, postgres_case):
    original, raw, _ = postgres_case
    value = saved_timeline(client, original, configured_body(raw))
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(text("TRUNCATE timeline_clip_visuals CASCADE"))
    assert client.get(save_url(value)).json() == value
