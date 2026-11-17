"""Keep schemas used by already frozen production versions byte compatible."""

import hashlib
import json

import pytest

from arclight.agents.story_outline import StoryOutlineContent
from arclight.domain.screenwriter_planning import ScreenwriterPlan


@pytest.mark.parametrize(
    "model,expected_digest",
    [
        (
            StoryOutlineContent,
            "70c55961f2c1c2638fa97cb2f67d200890a6c14325c7d6277aba2452877be468",
        ),
        (
            ScreenwriterPlan,
            "bee6f7c0b412cda4793e457b12f1854cfd2168d402b866d82413385554228912",
        ),
    ],
)
def test_series_extension_preserves_frozen_legacy_schemas(model, expected_digest):
    # Existing runs verify these definitions against their persisted receipts.
    # Extending their models would invalidate recovery even if JSON output omitted
    # empty new fields. SERIES-01 therefore has its own content model.
    encoded = json.dumps(
        model.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == expected_digest


def test_legacy_outline_serialization_keeps_original_idempotency_body():
    content = {
        "schema_version": "1.0",
        "target_episode_count": 1,
        "episode_duration_ms": 30000,
        "episodes": [
            {
                "episode_no": 1,
                "title": "找到信件",
                "goal": "确认信件的来历",
                "conflict": "唯一的证人拒绝交谈",
                "turning_point": "发现信封上的家族印记",
                "ending_hook": "印记来自早已消失的人",
            }
        ],
    }
    assert StoryOutlineContent.model_validate(content).model_dump(mode="json") == content
