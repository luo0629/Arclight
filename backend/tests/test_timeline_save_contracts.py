"""A save requires an explicit, strictly typed compare-and-append baseline."""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_timeline_contracts import snapshot

from arclight.domain.timelines import TimelineSave


@pytest.mark.parametrize("expected", [None, 0, -1, True, "1", 1.0])
def test_save_does_not_coerce_or_invent_a_baseline_version(expected):
    with pytest.raises(ValidationError):
        TimelineSave.model_validate(
            {
                "creation_key": str(uuid4()),
                "expected_version_no": expected,
                "snapshot": snapshot(),
            }
        )


def test_save_rejects_missing_baseline_and_untrusted_extra_commands():
    body = {"creation_key": str(uuid4()), "snapshot": snapshot()}
    with pytest.raises(ValidationError):
        TimelineSave.model_validate(body)
    body.update(expected_version_no=1, shell_command="arbitrary command")
    with pytest.raises(ValidationError):
        TimelineSave.model_validate(body)
