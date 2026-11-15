"""Public, bounded production state for an immutable storyboard page."""

from uuid import UUID

from pydantic import BaseModel

from arclight.domain.generation_requests import GenerationCreate


class ProductionBlocker(BaseModel):
    code: str
    message: str


class ProductionJob(BaseModel):
    id: UUID
    status: str
    revision: int
    attempt_count: int
    maximum_cost: str | None
    currency: str | None


class ProductionCandidate(BaseModel):
    id: UUID
    asset_id: UUID
    version_id: UUID
    version_no: int
    revision: int
    status: str
    image_path: str
    can_submit: bool
    can_approve: bool
    can_reject: bool
    blocker: ProductionBlocker | None


class ProductionShot(BaseModel):
    id: UUID
    shot_no: int
    description: str
    job: ProductionJob | None
    request: GenerationCreate | None
    blocker: ProductionBlocker | None
    candidates: list[ProductionCandidate]
    candidates_truncated: bool


class StoryboardProduction(BaseModel):
    project_id: UUID
    episode_id: UUID
    storyboard_id: UUID
    writable: bool
    owner: bool
    total: int
    offset: int
    next_offset: int | None
    shots: list[ProductionShot]
