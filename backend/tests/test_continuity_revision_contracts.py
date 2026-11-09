"""Pure contract checks preserve locked prompt blocks and finite command boundaries."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_generation_jobs import payload

from arclight.core.errors import DomainError
from arclight.domain.continuity_revisions import RevisionRequest, amend_prompt
from arclight.domain.generation_prompts import compose_image_prompt
from arclight.domain.generation_requests import GenerationSpec
from arclight.domain.versions import digest


def source_prompt():
    spec = GenerationSpec.model_validate(payload()["spec"] | {"batch_size": 3})
    visual = {
        "source_key": "visual_bible:original",
        "source_type": "VISUAL_BIBLE_VERSION",
        "version_id": str(uuid4()),
        "version_no": 1,
        "snapshot_sha256": "a" * 64,
        "fields": {"art_style": "ink", "prohibited": "watermark", "negative_prompt": "text"},
        "locked_fields": ["art_style", "prohibited"],
    }
    prompt, negative, composition = compose_image_prompt(
        prompt=spec.prompt,
        negative_prompt=spec.negative_prompt,
        context=spec.context,
        parameters=spec.parameters,
        output_spec=spec.output_spec,
        seed=spec.seed,
        variation=spec.variation,
        batch_size=spec.batch_size,
        asset_sources=[],
        visual_bible=visual,
    )
    return GenerationSpec.model_validate(
        spec.model_dump(mode="json") | {"prompt": prompt, "negative_prompt": negative}
    ), composition


@pytest.mark.parametrize(
    "block", ["shot_description", "camera_language", "continuity_context", "negative_constraints"]
)
def test_amendments_append_without_removing_locked_constraints_or_provider_parameters(block):
    spec, composition = source_prompt()
    before = deepcopy(composition)
    request = RevisionRequest(
        review_id=uuid4(),
        issue_ids=[uuid4()],
        action="REVISE_PROMPT",
        amendments=[{"block": block, "instruction": "Keep the red case closed."}],
    )
    revised, snapshot = amend_prompt(spec, composition, request)
    assert composition == before
    assert revised.batch_size == 1
    assert revised.model_dump(
        exclude={"prompt", "negative_prompt", "batch_size"}
    ) == spec.model_dump(exclude={"prompt", "negative_prompt", "batch_size"})
    assert snapshot["sources"][:-1] == composition["sources"]
    assert snapshot["provider_parameters"] == composition["provider_parameters"] | {"batch_size": 1}
    assert snapshot["prompt_sha256"] == digest(revised.prompt)
    assert snapshot["negative_prompt_sha256"] == digest(revised.negative_prompt)
    for old, new in zip(composition["blocks"], snapshot["blocks"], strict=True):
        if old["key"] == block:
            assert new["content"].startswith(old["content"])
            assert "Keep the red case closed." in new["content"]
            assert new["source_keys"][:-1] == old["source_keys"]
        else:
            assert old == new
    assert "watermark" in revised.negative_prompt and "ink" in revised.prompt


def test_regeneration_uses_frozen_prompt_and_only_one_candidate():
    spec, composition = source_prompt()
    request = RevisionRequest(review_id=uuid4(), issue_ids=[uuid4()], action="REGENERATE")
    revised, snapshot = amend_prompt(spec, composition, request)
    assert revised.prompt == spec.prompt and revised.negative_prompt == spec.negative_prompt
    assert snapshot["blocks"] == composition["blocks"]
    assert revised.batch_size == snapshot["provider_parameters"]["batch_size"] == 1


@pytest.mark.parametrize("field", ["prompt_sha256", "negative_prompt_sha256"])
def test_mismatched_composition_is_not_silently_reconstructed(field):
    spec, composition = source_prompt()
    composition[field] = "f" * 64
    request = RevisionRequest(review_id=uuid4(), issue_ids=[uuid4()], action="REGENERATE")
    with pytest.raises(DomainError) as error:
        amend_prompt(spec, composition, request)
    assert error.value.code == "REVISION_SOURCE_INVALID"


@pytest.mark.parametrize(
    "change",
    [
        {"issue_ids": []},
        {"issue_ids": [str(uuid4())] * 2},
        {"action": "APPROVE"},
        {"action": "REVISE_PROMPT"},
        {
            "amendments": [
                {"block": "character_constraints", "instruction": "replace locked references"}
            ]
        },
        {"amendments": [{"block": "continuity_context", "instruction": "x" * 2001}]},
        {"generation_spec": {}},
    ],
)
def test_revision_command_cannot_add_permissions_or_unbounded_generation_fields(change):
    with pytest.raises(ValidationError):
        RevisionRequest.model_validate(
            {"review_id": str(uuid4()), "issue_ids": [str(uuid4())], "action": "REGENERATE"}
            | change
        )
