"""Bounded revision commands; model advice never supplies a generation specification."""

import json
from copy import deepcopy
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.core.errors import DomainError
from arclight.domain.continuity_reviews import ReviewTarget
from arclight.domain.generation_prompts import BLOCK_LABELS, PROMPT_BLOCK_ORDER
from arclight.domain.generation_requests import GenerationSpec
from arclight.domain.production_state import ProductionContract
from arclight.domain.versions import digest

REVISION_RESOURCE = "CONTINUITY_REVISION"
MAX_REVISION_ROUNDS = 3


class PromptAmendment(ProductionContract):
    block: Literal[
        "shot_description", "camera_language", "continuity_context", "negative_constraints"
    ]
    instruction: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def meaningful_instruction(self):
        if not self.instruction.strip():
            raise ValueError("Revision instructions cannot be blank")
        return self


class RevisionRequest(ProductionContract):
    review_id: UUID
    issue_ids: list[UUID] = Field(min_length=1, max_length=32)
    action: Literal["REVISE_PROMPT", "REGENERATE"]
    amendments: list[PromptAmendment] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def bounded_scope(self):
        if len(set(self.issue_ids)) != len(self.issue_ids):
            raise ValueError("Revision issue identities must be unique")
        if len({item.block for item in self.amendments}) != len(self.amendments):
            raise ValueError("Each prompt block can be amended only once")
        if bool(self.amendments) != (self.action == "REVISE_PROMPT"):
            raise ValueError("Only prompt revisions require bounded amendments")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode()) > 16384:
            raise ValueError("Revision command exceeds 16 KiB")
        return self


class RevisionOrigin(ProductionContract):
    schema_version: Literal["continuity-revision/1"] = "continuity-revision/1"
    revision_id: UUID
    project_id: UUID
    request: RevisionRequest
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: ReviewTarget
    evidence_ids: list[UUID] = Field(min_length=1, max_length=128)
    source_job_id: UUID
    source_manifest_id: UUID
    root_candidate_id: UUID
    parent_revision_id: UUID | None
    round: int = Field(strict=True, ge=1, le=MAX_REVISION_ROUNDS + 1)
    max_rounds: Literal[3] = 3
    status: Literal["QUEUED", "NEEDS_REVIEW"]
    created_by: str
    created_at: datetime

    @model_validator(mode="after")
    def exact_command(self):
        if self.request_sha256 != digest(self.request.model_dump(mode="json")):
            raise ValueError("Revision command fingerprint does not match")
        queued = self.status == "QUEUED"
        if queued != (self.round <= self.max_rounds):
            raise ValueError("Revision round must respect the frozen limit")
        return self


class RevisionRecord(RevisionOrigin):
    job_id: UUID | None
    input_manifest_id: UUID | None

    @model_validator(mode="after")
    def exact_task(self):
        queued = self.status == "QUEUED"
        if (self.job_id is not None) != queued or (self.input_manifest_id is not None) != queued:
            raise ValueError("Only admitted revisions have a generation task")
        return self


def amend_prompt(
    original: GenerationSpec, composition: dict, request: RevisionRequest
) -> tuple[GenerationSpec, dict]:
    """Append corrections to allowed blocks, retaining every original constraint and source."""
    value = deepcopy(composition)
    if (
        value.get("prompt_sha256") != digest(original.prompt)
        or value.get("negative_prompt_sha256") != digest(original.negative_prompt or "")
        or [block["key"] for block in value.get("blocks", [])] != list(PROMPT_BLOCK_ORDER)
    ):
        raise DomainError("REVISION_SOURCE_INVALID", "原提示词与组合清单不一致", 409)
    source_key = f"continuity_review:{request.review_id}"
    for amendment in request.amendments:
        block = next(block for block in value["blocks"] if block["key"] == amendment.block)
        block["content"] = "\n".join(
            part
            for part in (block["content"], "修订要求：" + amendment.instruction.strip())
            if part
        )
        block["source_keys"] = list(dict.fromkeys([*block["source_keys"], source_key]))
    prompt = "\n\n".join(
        f"【{BLOCK_LABELS[block['key']]}】\n{block['content']}"
        for block in value["blocks"]
        if block["content"] and block["key"] != "negative_constraints"
    )
    negative = next(
        block["content"] for block in value["blocks"] if block["key"] == "negative_constraints"
    )
    # Replace one problematic candidate, even when its source task generated a batch.
    spec = GenerationSpec.model_validate(
        original.model_dump(mode="json")
        | {"prompt": prompt, "negative_prompt": negative, "batch_size": 1}
    )
    value.update(
        negative_prompt=negative,
        prompt_sha256=digest(prompt),
        negative_prompt_sha256=digest(negative),
        provider_parameters={**value["provider_parameters"], "batch_size": 1},
    )
    value["sources"].append(
        {
            "source_key": source_key,
            "source_type": "CONTINUITY_REVIEW",
            "review_id": str(request.review_id),
            "issue_ids": [str(identity) for identity in request.issue_ids],
        }
    )
    if len(json.dumps(value, ensure_ascii=False).encode()) > 65536:
        raise DomainError("PROMPT_COMPOSITION_TOO_LARGE", "修订后的提示词组合清单超限", 422)
    return spec, value
