"""Pure narrative ledger boundary tests; database admission is tested separately."""

import json
from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from arclight.core.errors import DomainError
from arclight.domain import story_ledger
from arclight.domain.story_ledger import (
    MAX_QUERY_BYTES,
    LedgerEntry,
    LedgerQuery,
    StoryLedgerContent,
    query_ledger,
)


def uid(number):
    return UUID(int=number)


def scene(number=1, **changes):
    return {
        "id": uid(number),
        "episode_id": uid(100 + number),
        "episode_no": number,
        "script_version_id": uid(200 + number),
        "scene_id": uid(300 + number),
        "scene_no": 1,
        "segment_no": 1,
        "story_time": number * 10,
    } | changes


def entry(mapping, number=1, **changes):
    return {
        "id": uid(1000 + number),
        "kind": "EVENT",
        "status": "CONFIRMED",
        "state_key": "event",
        "value": f"fact-{number}",
        "story_start": 0,
        "disclosure_start": mapping["id"],
        "source_script_version_id": mapping["script_version_id"],
        "source_scene_id": mapping["scene_id"],
    } | changes


def content(mappings, entries):
    return StoryLedgerContent.model_validate({"scene_times": mappings, "entries": entries})


def request(mapping, **changes):
    return LedgerQuery.model_validate(
        {
            "episode_id": mapping["episode_id"],
            "script_version_id": mapping["script_version_id"],
            "scene_id": mapping["scene_id"],
            "segment_no": mapping["segment_no"],
            "perspective": "AUTHOR",
        }
        | changes
    )


def identities(result, group="facts"):
    return {UUID(item["id"]) for item in result[group]}


def test_only_approved_root_confirmed_entries_are_facts():
    mapping = scene()
    snapshot = content(
        [mapping],
        [
            entry(mapping),
            entry(mapping, 2, status="PROPOSED"),
            entry(mapping, 3, status="AUTHOR_PLANNED"),
        ],
    )
    query = request(mapping, include_planned=True, include_proposed=True)
    approved = query_ledger(snapshot, query, approved_root=True)
    assert identities(approved) == {uid(1001)}
    assert identities(approved, "planned") == {uid(1003)}
    assert identities(approved, "proposals") == {uid(1002)}
    unapproved = query_ledger(snapshot, query, approved_root=False)
    assert unapproved["facts"] == []
    assert identities(unapproved, "proposals") == {uid(1001), uid(1002)}
    assert {item["status"] for item in unapproved["proposals"]} == {"PROPOSED"}
    assert query_ledger(snapshot, request(mapping), approved_root=False)["proposals"] == []


def test_future_facts_and_proposals_do_not_leak_but_author_can_explicitly_read_plans():
    early, later = scene(1), scene(2)
    snapshot = content(
        [early, later],
        [
            entry(later),
            entry(later, 2, status="PROPOSED"),
            entry(
                later,
                3,
                status="AUTHOR_PLANNED",
                story_start=100,
                source_script_version_id=None,
                source_scene_id=None,
            ),
        ],
    )
    implicit = query_ledger(snapshot, request(early), approved_root=True)
    assert implicit["facts"] == implicit["planned"] == implicit["proposals"] == []
    explicit = query_ledger(
        snapshot, request(early, include_planned=True, include_proposed=True), approved_root=True
    )
    assert explicit["facts"] == explicit["proposals"] == []
    assert identities(explicit, "planned") == {uid(1003)}
    assert explicit["planned"][0]["status"] == "AUTHOR_PLANNED"


@pytest.mark.parametrize("story_time,expected", [(9, False), (10, True), (19, True), (20, False)])
def test_character_knowledge_has_half_open_acquisition_and_forgetting_bounds(story_time, expected):
    mapping = scene(story_time=story_time)
    snapshot = content(
        [mapping],
        [
            entry(
                mapping,
                known_by=[{"character_id": uid(501), "story_start": 10, "story_end": 20}],
            )
        ],
    )
    query = request(mapping, perspective="CHARACTER", character_id=uid(501))
    result = query_ledger(snapshot, query, approved_root=True)
    assert bool(result["facts"]) is expected
    assert "known_by" not in json.dumps(result)
    other = request(mapping, perspective="CHARACTER", character_id=uid(502))
    assert query_ledger(snapshot, other, approved_root=True)["facts"] == []


def test_known_by_default_is_no_character_knowledge():
    mapping = scene()
    snapshot = content([mapping], [entry(mapping)])
    assert (
        query_ledger(
            snapshot,
            request(mapping, perspective="CHARACTER", character_id=uid(501)),
            approved_root=True,
        )["facts"]
        == []
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"perspective": "CHARACTER"},
        {"perspective": "CHARACTER", "character_id": uid(501), "include_planned": True},
        {"perspective": "CHARACTER", "character_id": uid(501), "include_proposed": True},
        {"character_id": uid(501)},
        {"story_time": 1_000_000},
        {"approved_root": True},
        {"limit": 101},
        {"include_planned": "false"},
        {"segment_no": True},
    ],
)
def test_query_rejects_missing_perspective_or_client_authority_and_time(changes):
    with pytest.raises(ValidationError):
        request(scene(), **changes)


def test_flashback_and_flashforward_use_explicit_story_time_and_presentation():
    initial = scene(1, story_time=50)
    flashback = scene(2, story_time=5)
    flashforward = scene(3, story_time=100)
    snapshot = content(
        [initial, flashback, flashforward],
        [
            entry(initial, story_start=20),
            entry(flashback, 2, story_start=0),
            entry(flashforward, 3, story_start=80),
        ],
    )
    assert identities(query_ledger(snapshot, request(initial), approved_root=True)) == {uid(1001)}
    assert identities(query_ledger(snapshot, request(flashback), approved_root=True)) == {uid(1002)}
    assert identities(query_ledger(snapshot, request(flashforward), approved_root=True)) == {
        uid(1001),
        uid(1002),
        uid(1003),
    }


def test_multiple_segments_in_one_exact_scene_do_not_implicitly_share_time():
    first = scene(story_time=30)
    second = first | {"id": uid(2), "segment_no": 2, "story_time": 5}
    snapshot = content([first, second], [entry(first, story_start=20)])
    assert identities(query_ledger(snapshot, request(first), approved_root=True)) == {uid(1001)}
    assert query_ledger(snapshot, request(second), approved_root=True)["facts"] == []


@pytest.mark.parametrize("field", ["episode_id", "script_version_id", "scene_id", "segment_no"])
def test_query_never_falls_back_from_unknown_exact_target(field):
    mapping = scene()
    snapshot = content([mapping], [entry(mapping)])
    changed = 2 if field == "segment_no" else uid(9999)
    with pytest.raises(DomainError) as error:
        query_ledger(snapshot, request(mapping, **{field: changed}), approved_root=True)
    assert error.value.code == "STORY_LEDGER_TARGET_INVALID"


@pytest.mark.parametrize("story_time,expected", [(9, False), (10, True), (19, True), (20, False)])
def test_story_state_intervals_are_half_open(story_time, expected):
    mapping = scene(story_time=story_time)
    snapshot = content([mapping], [entry(mapping, story_start=10, story_end=20)])
    result = query_ledger(snapshot, request(mapping), approved_root=True)
    assert bool(result["facts"]) is expected


def test_disclosure_interval_excludes_its_end():
    start, end = scene(1), scene(2)
    snapshot = content([start, end], [entry(start, disclosure_end=end["id"])])
    assert query_ledger(snapshot, request(start), approved_root=True)["facts"]
    assert query_ledger(snapshot, request(end), approved_root=True)["facts"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"source_script_version_id": None},
        {"source_scene_id": None},
        {"source_scene_id": uid(9999)},
        {"disclosure_start": uid(9999)},
        {"disclosure_end": uid(9999)},
        {"disclosure_end": uid(1)},
        {"story_start": 10, "story_end": 10},
        {"story_start": 11, "story_end": 10},
        {"kind": "INJURY"},
        {"kind": "RELATIONSHIP", "subject_id": uid(501)},
    ],
)
def test_entry_and_snapshot_reject_invalid_source_scope_and_identity(change):
    mapping = scene()
    with pytest.raises(ValidationError):
        content([mapping], [entry(mapping, **change)])


@pytest.mark.parametrize("status", ["PROPOSED", "CONFIRMED"])
def test_only_plans_may_omit_the_exact_source(status):
    mapping = scene()
    with pytest.raises(ValidationError, match="require an exact source"):
        content(
            [mapping],
            [entry(mapping, status=status, source_script_version_id=None, source_scene_id=None)],
        )
    planned = entry(
        mapping,
        kind="INJURY",
        status="AUTHOR_PLANNED",
        source_script_version_id=None,
        source_scene_id=None,
    )
    assert content([mapping], [planned]).entries[0].subject_id is None


@pytest.mark.parametrize(
    "changes",
    [
        {"id": uid(1)},
        {"episode_no": 1},
        {"episode_id": uid(101)},
        {"script_version_id": uid(201)},
    ],
)
def test_duplicate_or_inconsistent_mapping_identities_are_rejected(changes):
    first = scene(1)
    second = scene(2, **changes)
    with pytest.raises(ValidationError):
        content([first, second], [])


def test_two_script_versions_cannot_claim_the_same_presentation_position():
    first = scene(1)
    second = first | {"id": uid(2), "script_version_id": uid(202)}
    with pytest.raises(ValidationError, match="presentation position"):
        content([first, second], [])


@pytest.mark.parametrize("kind", ["INJURY", "COSTUME", "KNOWLEDGE", "PROP_OWNERSHIP"])
def test_overlapping_confirmed_state_values_conflict(kind):
    mapping = scene()
    original = entry(mapping, kind=kind, subject_id=uid(501), value="before")
    replacement = entry(mapping, 2, kind=kind, subject_id=uid(501), value="after")
    with pytest.raises(ValidationError, match="state values conflict"):
        content([mapping], [original, replacement])
    replacement["status"] = "PROPOSED"
    assert len(content([mapping], [original, replacement]).entries) == 2


def test_prop_ownership_conflicts_even_when_value_text_matches_but_owner_differs():
    mapping = scene()
    original = entry(
        mapping, kind="PROP_OWNERSHIP", subject_id=uid(601), target_id=uid(501), value="held"
    )
    replacement = original | {"id": uid(1002), "target_id": uid(502)}
    with pytest.raises(ValidationError, match="state values conflict"):
        content([mapping], [original, replacement])


def test_state_changes_at_adjacent_story_or_disclosure_bounds_are_legal():
    first, second = scene(1), scene(2)
    original = entry(first, kind="COSTUME", subject_id=uid(501), story_end=20)
    replacement = original | {"id": uid(1002), "story_start": 20, "story_end": None, "value": "new"}
    assert len(content([first, second], [original, replacement]).entries) == 2
    original = original | {"story_end": None, "disclosure_end": second["id"]}
    replacement = replacement | {"story_start": 0, "disclosure_start": second["id"]}
    assert len(content([first, second], [original, replacement]).entries) == 2


def test_relationships_are_directed_and_distinct_targets_do_not_conflict():
    mapping = scene()
    first = entry(
        mapping, kind="RELATIONSHIP", subject_id=uid(501), target_id=uid(502), value="trusts"
    )
    second = first | {
        "id": uid(1002),
        "subject_id": uid(502),
        "target_id": uid(501),
        "value": "distrusts",
    }
    third = first | {"id": uid(1003), "target_id": uid(503), "value": "distrusts"}
    assert len(content([mapping], [first, second, third]).entries) == 3
    with pytest.raises(ValidationError):
        content([mapping], [first | {"target_id": uid(501)}])


@pytest.mark.parametrize(
    "change",
    [
        {"causes": [uid(9999)]},
        {"causes": [uid(1001)]},
        {"causes": [uid(1002), uid(1002)]},
    ],
)
def test_dangling_self_and_duplicate_causes_are_rejected(change):
    mapping = scene()
    with pytest.raises(ValidationError):
        content([mapping], [entry(mapping, **change), entry(mapping, 2)])


def test_causal_cycle_and_future_or_unconfirmed_causes_are_rejected():
    mapping = scene()
    first = entry(mapping, causes=[uid(1002)])
    second = entry(mapping, 2, causes=[uid(1001)])
    with pytest.raises(ValidationError, match="cycles"):
        content([mapping], [first, second])
    second["causes"] = []
    second["story_start"] = 20
    with pytest.raises(ValidationError, match="cannot occur after"):
        content([mapping], [first, second])
    second["story_start"] = 0
    second["status"] = "PROPOSED"
    with pytest.raises(ValidationError, match="unconfirmed cause"):
        content([mapping], [first, second])


def test_hidden_causes_and_knowledge_metadata_do_not_escape_query_projection():
    early, later = scene(1), scene(2)
    known = [{"character_id": uid(501), "story_start": 0}]
    snapshot = content(
        [early, later],
        [
            entry(later, value="HIDDEN_SECRET", known_by=known),
            entry(early, 2, value="visible effect", known_by=known, causes=[uid(1001)]),
        ],
    )
    query = request(early, perspective="CHARACTER", character_id=uid(501))
    result = query_ledger(snapshot, query, approved_root=True)
    serialized = json.dumps(result)
    assert identities(result) == {uid(1002)}
    assert result["facts"][0]["causes"] == []
    assert "known_by" not in serialized and "HIDDEN_SECRET" not in serialized
    assert str(uid(1001)) not in serialized
    later_result = query_ledger(snapshot, request(later), approved_root=True)
    effect = next(item for item in later_result["facts"] if item["id"] == str(uid(1002)))
    assert effect["causes"] == [str(uid(1001))]


def test_query_is_stable_bounded_and_never_truncates_exact_entry_sources():
    mapping = scene()
    snapshot = content(
        [mapping],
        [entry(mapping, number, value="界" * 2000) for number in range(1, 101)],
    )
    before = deepcopy(snapshot.model_dump(mode="json"))
    query = request(mapping)
    result = query_ledger(snapshot, query, approved_root=True)
    assert result["truncated"]
    assert 0 < len(result["facts"]) < 100
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_QUERY_BYTES
    for item in result["facts"]:
        assert item["value"] == "界" * 2000
        assert item["source_script_version_id"] == str(mapping["script_version_id"])
        assert item["source_scene_id"] == str(mapping["scene_id"])
    assert result == query_ledger(snapshot, query, approved_root=True)
    assert snapshot.model_dump(mode="json") == before


def test_limit_and_budget_filter_causal_references_after_selection():
    mapping = scene()
    snapshot = content(
        [mapping],
        [
            entry(mapping, causes=[uid(1002)]),
            entry(mapping, 2),
        ],
    )
    result = query_ledger(snapshot, request(mapping, limit=1), approved_root=True)
    assert result["truncated"] and len(result["facts"]) == 1
    assert result["facts"][0]["causes"] == []


def test_contract_bounds_text_snapshot_bytes_and_collection_sizes(monkeypatch):
    mapping = scene()
    with pytest.raises(ValidationError):
        LedgerEntry.model_validate(entry(mapping, value="x" * 2001))
    with pytest.raises(ValidationError):
        StoryLedgerContent.model_validate({"scene_times": [mapping] * 2001})
    with pytest.raises(ValidationError):
        content([mapping], [entry(mapping)] * 1001)
    monkeypatch.setattr(story_ledger, "MAX_LEDGER_BYTES", 10)
    with pytest.raises(ValidationError, match="byte limit"):
        content([mapping], [entry(mapping)])


def test_duplicate_entries_and_overlapping_knowledge_are_rejected():
    mapping = scene()
    with pytest.raises(ValidationError, match="identities must be unique"):
        content([mapping], [entry(mapping), entry(mapping)])
    with pytest.raises(ValidationError, match="Knowledge intervals"):
        content(
            [mapping],
            [
                entry(
                    mapping,
                    known_by=[
                        {"character_id": uid(501), "story_start": 0, "story_end": 10},
                        {"character_id": uid(501), "story_start": 9},
                    ],
                )
            ],
        )


def test_pagination_totals_count_only_filtered_visible_entries():
    early, later = scene(1), scene(2)
    snapshot = content(
        [early, later],
        [entry(early, number) for number in range(1, 4)] + [entry(later, 4)],
    )
    page = query_ledger(snapshot, request(early, limit=1, offset=1), approved_root=True)
    assert page["total"] == 3 and page["limit"] == 1 and page["offset"] == 1
    assert identities(page) == {uid(1002)} and page["truncated"]
    assert page["returned_count"] == 1 and page["next_offset"] == 2
    last = query_ledger(snapshot, request(early, limit=1, offset=2), approved_root=True)
    assert last["total"] == 3 and not last["truncated"]
    assert last["returned_count"] == 1 and last["next_offset"] is None
    empty = query_ledger(snapshot, request(early, limit=1, offset=3), approved_root=True)
    assert empty["facts"] == [] and empty["total"] == 3 and not empty["truncated"]
    assert empty["returned_count"] == 0 and empty["next_offset"] is None
    with pytest.raises(ValidationError):
        request(early, offset=-1)
    with pytest.raises(ValidationError):
        request(early, offset=1001)


def test_budget_pagination_visits_every_visible_entry_without_duplicates_or_gaps():
    early, later = scene(1), scene(2)
    visible = (
        [entry(early, number, value="界" * 2000) for number in range(1, 31)]
        + [entry(early, number, status="PROPOSED", value="提" * 2000) for number in range(31, 36)]
        + [
            entry(later, number, status="AUTHOR_PLANNED", story_start=100, value="计" * 2000)
            for number in range(36, 41)
        ]
    )
    hidden = [entry(later, number, value="future secret") for number in range(41, 44)]
    snapshot = content([early, later], visible + hidden)
    visited = []
    offset = 0
    pages = 0
    for _ in range(len(visible) + 1):
        page = query_ledger(
            snapshot,
            request(early, include_planned=True, include_proposed=True, limit=100, offset=offset),
            approved_root=True,
        )
        rows = page["facts"] + page["planned"] + page["proposals"]
        assert 0 < page["returned_count"] == len(rows) < page["limit"]
        assert page["total"] == len(visible)
        assert page["offset"] == offset
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= MAX_QUERY_BYTES
        visited.extend(UUID(item["id"]) for item in rows)
        pages += 1
        if page["next_offset"] is None:
            assert not page["truncated"]
            break
        assert page["truncated"]
        assert page["next_offset"] == offset + page["returned_count"]
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    else:
        pytest.fail("Bounded pagination did not reach the final page")
    assert pages > 1
    assert len(visited) == len(set(visited)) == len(visible)
    assert set(visited) == {item["id"] for item in visible}
    assert not set(visited) & {item["id"] for item in hidden}
