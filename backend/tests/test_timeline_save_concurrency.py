"""Real PostgreSQL CAS races; each request uses an independent HTTP app and transaction."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from starlette.testclient import TestClient
from test_concurrency import require_postgres
from test_continuity_reviews import review_case  # noqa: F401
from test_timeline_saves import duration_edit, save_url, saved_case  # noqa: F401
from test_timelines import timeline_case  # noqa: F401

from arclight.main import create_app
from arclight.persistence.models import AuditEvent
from arclight.persistence.timelines import Timeline, TimelineRevision

__all__ = ["review_case", "timeline_case", "saved_case"]


@pytest.fixture
def postgres_case(settings, request):
    require_postgres(settings)
    return request.getfixturevalue("saved_case")


@pytest.mark.parametrize("kind", ["competing", "duplicate", "same_key_changed_body"])
def test_postgres_compare_and_append_has_one_winner_and_idempotent_retry(
    client, postgres_case, kind
):
    original, first, _ = postgres_case
    second = deepcopy(first)
    if kind != "duplicate":
        second = duration_edit(second, 2000)
    if kind == "competing":
        second["creation_key"] = str(uuid4())
    barrier = Barrier(2)

    def save(body):
        with TestClient(create_app(client.app.state.settings)) as connection:
            connection.headers.update(dict(client.headers))
            barrier.wait(timeout=20)
            return connection.patch(save_url(original), json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, [first, second]))
    if kind == "duplicate":
        assert [response.status_code for response in responses] == [201, 201], [
            r.text for r in responses
        ]
        assert responses[0].json() == responses[1].json()
    else:
        assert sorted(response.status_code for response in responses) == [201, 409], [
            r.text for r in responses
        ]
        loser = next(response for response in responses if response.status_code == 409)
        assert loser.json()["error"]["code"] == (
            "VERSION_CONFLICT" if kind == "competing" else "IDEMPOTENCY_CONFLICT"
        )
    winner = next(response.json() for response in responses if response.status_code == 201)
    assert winner["version_no"] == 2
    assert winner["based_on_timeline_id"] == original["id"]
    assert client.get(save_url(original)).json() == original
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(Timeline.id))) == 2
        assert db.scalar(select(func.count(TimelineRevision.id))) == 1
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == "timeline.saved")
            )
            == 1
        )
        assert db.get(TimelineRevision, UUID(winner["id"])).based_on_timeline_id == UUID(
            original["id"]
        )


def test_postgres_save_history_cannot_be_truncated(client, postgres_case):
    original, body, _ = postgres_case
    result = client.patch(save_url(original), json=body)
    assert result.status_code == 201, result.text
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(text("TRUNCATE timeline_revisions CASCADE"))
    assert client.get(save_url(original)).json() == original
    assert client.get(save_url(result.json())).json() == result.json()
