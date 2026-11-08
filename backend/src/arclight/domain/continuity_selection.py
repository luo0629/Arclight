"""Admission evidence consumed by selection; only the domain router may issue it."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from arclight.domain.production_state import ProductionContract
from arclight.domain.workflow_contracts import Checksum

ADMISSION_RESOURCE = "REVIEW_ADMISSION"
HUMAN_REVIEW_HANDLER = "continuity_shot"


class ReviewAdmission(ProductionContract):
    """An exact routed receipt, distinct from QA-02's advisory contract artifact.

    QA-04 will issue these through its trusted domain service. There is no HTTP
    creation endpoint; QA-07 exercises the consumer with persisted contract fixtures.
    The human request binds the complete review receipt, including its rerun.
    """

    schema_version: Literal["continuity-admission/1"] = "continuity-admission/1"
    review_id: UUID
    record_sha256: Checksum
    requires_human_review: bool = Field(strict=True)
    human_review_request_id: UUID | None = None
