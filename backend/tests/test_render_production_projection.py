"""Production state consumes exact, current timeline selections without inventing QA."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_timelines import created_timeline, review_case, timeline_case

from arclight.application.production_state import shot_facts
from arclight.persistence.models import Episode, Shot
from arclight.persistence.timelines import TimelineClip

__all__ = ["review_case", "timeline_case"]


@pytest.mark.parametrize("changed", ["source_sha256", "review_id", "shot_id"])
def test_timeline_shot_projection_requires_exact_frozen_selection_receipt(
    client, episode, timeline_case, changed
):
    body, _ = timeline_case
    timeline = created_timeline(client, episode, body)
    with client.app.state.session_factory() as db:
        owner_episode = db.get(Episode, UUID(episode["id"]))
        clip = db.scalar(
            select(TimelineClip)
            .where(TimelineClip.timeline_id == UUID(timeline["id"]))
            .order_by(TimelineClip.ordinal)
        )
        shot = db.get(Shot, clip.shot_id)
        # The immutable storyboard itself carries no selected_visual_asset field.
        assert shot_facts(db, owner_episode, shot).media is None
        exact = shot_facts(db, owner_episode, shot, clip)
        assert exact.file_check is not None and exact.file_check.decision == "PASS"
        assert exact.continuity is not None and exact.continuity.review_id == clip.review_id
        assert exact.selection is not None and exact.selection.review_id == clip.id
        assert shot.storyboard_id in exact.media.source_version_ids

        altered = deepcopy(clip)
        setattr(altered, changed, "0" * 64 if changed == "source_sha256" else uuid4())
        rejected = shot_facts(db, owner_episode, shot, altered)
        assert rejected.continuity is None
        assert rejected.selection is None
