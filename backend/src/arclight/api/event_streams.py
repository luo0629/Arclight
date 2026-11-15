import asyncio
from collections.abc import AsyncIterator
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.concurrency import run_in_threadpool

from arclight.api.dependencies import current_actor, security
from arclight.application.event_streams import Cursor, EventPage, read_events
from arclight.core.errors import DomainError

router = APIRouter(prefix="/api/v1", tags=["events"])
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(security)]
LastEventID = Annotated[str | None, Header(alias="Last-Event-ID", max_length=160)]
POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 10.0
CONNECTION_SECONDS = 55.0


def load_page(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    project_id: UUID | None,
    run_id: UUID | None,
    cursor: str | None,
    tail: bool = False,
) -> EventPage:
    # Recheck the *current* session/token and membership in a fresh transaction.
    # No DB connection, ORM row or lock lives across a network yield or wait.
    with request.app.state.session_factory.begin() as db:
        actor = current_actor(request, db, credentials)
        return read_events(
            db,
            actor,
            project_id=project_id,
            run_id=run_id,
            cursor=Cursor.parse(cursor),
            tail=tail,
        )


async def stream(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    project_id: UUID | None,
    run_id: UUID | None,
    last_id: str | None,
    first: EventPage,
) -> AsyncIterator[ServerSentEvent]:
    started = monotonic()
    heartbeat = started
    page = first
    yield ServerSentEvent(
        event="stream.ready",
        data={"project_id": str(page.project_id)},
        retry=1000,
        id=("" if page.reset else last_id or page.cursor or ""),
    )
    while not await request.is_disconnected():
        if page.reset:
            yield ServerSentEvent(
                event="stream.reset", id="", data={"project_id": str(page.project_id)}
            )
        for event in page.events:
            yield ServerSentEvent(event=event.event, id=event.cursor, data=event.data)
        if monotonic() - started >= CONNECTION_SECONDS:
            yield ServerSentEvent(event="stream.reconnect", data={})
            return
        if monotonic() - heartbeat >= HEARTBEAT_SECONDS:
            yield ServerSentEvent(comment="keep-alive")
            heartbeat = monotonic()
        await asyncio.sleep(0 if page.has_more else POLL_SECONDS)
        if await request.is_disconnected():
            return
        try:
            page = await run_in_threadpool(
                load_page, request, credentials, project_id, run_id, page.cursor
            )
        except DomainError as error:
            yield ServerSentEvent(
                event="stream.error", data={"code": error.code, "status": error.status_code}
            )
            return
        except Exception:
            yield ServerSentEvent(
                event="stream.error", data={"code": "SERVICE_UNAVAILABLE", "status": 503}
            )
            return


async def first_project_page(
    project_id: UUID,
    request: Request,
    response: Response,
    credentials: Credentials,
    last_id: LastEventID = None,
) -> EventPage:
    response.headers["Cache-Control"] = "no-cache, no-store"
    return await run_in_threadpool(
        load_page, request, credentials, project_id, None, last_id, not bool(last_id)
    )


async def first_run_page(
    run_id: UUID,
    request: Request,
    response: Response,
    credentials: Credentials,
    last_id: LastEventID = None,
) -> EventPage:
    response.headers["Cache-Control"] = "no-cache, no-store"
    return await run_in_threadpool(
        load_page, request, credentials, None, run_id, last_id, not bool(last_id)
    )


@router.get("/projects/{project_id}/events/stream", response_class=EventSourceResponse)
async def project_events(
    project_id: UUID,
    request: Request,
    credentials: Credentials,
    first: Annotated[EventPage, Depends(first_project_page)],
    last_id: LastEventID = None,
) -> AsyncIterator[ServerSentEvent]:
    # The dependency authenticates and validates before HTTP headers are sent.
    # Yield through FastAPI's native SSE encoder, including its heartbeat support.
    async for event in stream(request, credentials, project_id, None, last_id, first):
        yield event


@router.get("/runs/{run_id}/events/stream", response_class=EventSourceResponse)
async def run_events(
    run_id: UUID,
    request: Request,
    credentials: Credentials,
    first: Annotated[EventPage, Depends(first_run_page)],
    last_id: LastEventID = None,
) -> AsyncIterator[ServerSentEvent]:
    async for event in stream(request, credentials, None, run_id, last_id, first):
        yield event
