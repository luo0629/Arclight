"""Server-issued routing receipt; a PASS only opens the creator's review gate."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import model_validator

from arclight.domain.production_state import ProductionContract
from arclight.domain.workflow_contracts import Checksum


class ReviewRoute(ProductionContract):
    schema_version: Literal["continuity-route/1"] = "continuity-route/1"
    review_id: UUID
    record_sha256: Checksum
    run_id: UUID
    loop_id: UUID
    conclusion: Literal["PASS", "REVISION_REQUIRED", "HUMAN_REVIEW"]
    destination: Literal["READY_FOR_REVIEW", "REVISION_REQUIRED", "HUMAN_REVIEW"]
    event_id: UUID
    routed_by: str
    routed_at: datetime

    @model_validator(mode="after")
    def exact_route(self):
        expected = "READY_FOR_REVIEW" if self.conclusion == "PASS" else self.conclusion
        if self.destination != expected or self.event_id != uuid5(
            self.review_id, "production-event:1:REVIEW:1"
        ):
            raise ValueError("Route must preserve the conclusion and stable event identity")
        return self
