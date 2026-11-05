"""Frozen workflow-only Outbox writer for explicitly marked pre-0048 fixtures."""

from sqlalchemy import Column, MetaData, Table
from sqlalchemy.orm import registry

from arclight.persistence.models import OutboxEvent as CurrentOutboxEvent

_table = Table(
    "outbox_events",
    MetaData(),
    *(
        Column(
            column.name,
            column.type,
            primary_key=column.primary_key,
            nullable=column.nullable,
            default=column.default,
            onupdate=column.onupdate,
        )
        for column in CurrentOutboxEvent.__table__.columns
        if column.name != "production_event_id"
    ),
)


class OutboxEvent:
    pass


registry().map_imperatively(OutboxEvent, _table)
