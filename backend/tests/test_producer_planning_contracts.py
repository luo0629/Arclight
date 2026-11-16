import json
from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from arclight.domain.art_concepts import ArtConceptPlan
from arclight.domain.art_direction import ArtDirectionTask
from arclight.domain.producer_planning import (
    DELEGATE_TOOL,
    PLAN_VERSION,
    WORKFLOW_VERSION,
    ArtTask,
    DelegationRequest,
    DelegationResult,
    DirectorTask,
    ProducerGoal,
    ProducerPlan,
    ProducerPlanReference,
    ReviewerTask,
    WriterTask,
)

SOURCE = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
THIRD = "33333333-3333-4333-8333-333333333333"
HASH = "a" * 64


def task(key="write", *, depends_on=None, payload=None):
    return {
        "task_key": key,
        "depends_on": depends_on or [],
        "reason": "完成当前目标所需的专业工作",
        "task": payload or {"kind": "screenwriter", "story": "一个有因果和结尾的故事"},
    }


def plan_payload(*tasks):
    return {
        "schema_version": PLAN_VERSION,
        "goal_sha256": HASH,
        "summary": "依据当前状态委派专业任务",
        "evidence": [{"tool_call_key": "investigate:overview-1", "evidence_sha256": HASH}],
        "tasks": list(tasks) or [task()],
    }


def test_plan_preserves_four_typed_specialists_and_exact_inputs():
    payload = plan_payload(
        task(),
        task(
            "direct",
            depends_on=["write"],
            payload={"kind": "director", "script_version_id": SOURCE},
        ),
        task(
            "art",
            depends_on=["write"],
            payload={
                "kind": "art_director",
                "script_version_id": SOURCE,
                "task": {"visual_goal": "保持角色服装一致"},
            },
        ),
        task(
            "review",
            depends_on=["direct", "art"],
            payload={
                "kind": "continuity_reviewer",
                "candidate_id": SOURCE,
                "storyboard_id": OTHER,
                "shot_id": THIRD,
            },
        ),
    )
    parsed = ProducerPlan.model_validate(payload)
    assert [type(item.task) for item in parsed.tasks] == [
        WriterTask,
        DirectorTask,
        ArtTask,
        ReviewerTask,
    ]
    assert parsed.tasks[1].task.script_version_id == UUID(SOURCE)
    assert parsed.tasks[2].task.task == ArtDirectionTask(visual_goal="保持角色服装一致")
    assert parsed.tasks[2].task.concept_plan is None
    assert parsed.tasks[3].task.shot_id == UUID(THIRD)
    assert ProducerPlan.model_validate_json(parsed.model_dump_json()) == parsed


def test_plan_reuses_bounded_concept_contract_without_a_parallel_service_task():
    concept = {
        "spec": {
            "model": "fake-image",
            "model_config_version": "1",
            "prompt": "服装参考",
            "prompt_version": "1",
            "output_spec": {"width": 512, "height": 512},
        },
        "maximum_cost": "0.25",
    }
    payload = plan_payload(
        task(
            "art",
            payload={
                "kind": "art_director",
                "script_version_id": SOURCE,
                "task": {"visual_goal": "比较两种服装"},
                "concept_plan": concept,
            },
        )
    )
    assert ProducerPlan.model_validate(payload).tasks[0].task.concept_plan == (
        ArtConceptPlan.model_validate(concept)
    )
    payload["tasks"][0]["task"]["concept_plan"]["maximum_candidates"] = 0
    with pytest.raises(ValidationError):
        ProducerPlan.model_validate(payload)


@pytest.mark.parametrize("kind", ["producer", "generation", "render", "shell", "sql"])
def test_plan_rejects_unregistered_task_types(kind):
    with pytest.raises(ValidationError):
        ProducerPlan.model_validate(plan_payload(task(payload={"kind": kind})))


@pytest.mark.parametrize("field", ["limits", "actor", "project_id", "parameters"])
@pytest.mark.parametrize("location", ["plan", "entry", "task"])
def test_model_cannot_supply_authority_or_arbitrary_arguments(field, location):
    payload = plan_payload()
    target = {
        "plan": payload,
        "entry": payload["tasks"][0],
        "task": payload["tasks"][0]["task"],
    }[location]
    target[field] = {"maximum_cost": "1000000"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProducerPlan.model_validate(payload)


@pytest.mark.parametrize(
    "tasks",
    [
        [task(), task()],
        [task(depends_on=["write"])],
        [task(depends_on=["missing"])],
        [task("write", depends_on=["direct"]), task("direct")],
        [task("write", depends_on=["direct"]), task("direct", depends_on=["write"])],
        [task(), task("direct", depends_on=["write", "write"])],
    ],
    ids=["duplicate-key", "self", "missing", "forward", "cycle", "duplicate-edge"],
)
def test_plan_rejects_ambiguous_or_nonpreceding_dependencies(tasks):
    with pytest.raises(ValidationError):
        ProducerPlan.model_validate(plan_payload(*tasks))


@pytest.mark.parametrize("key", ["", "A", "../run", "a b", "脚本", "a" * 65])
def test_task_keys_remain_bounded_stable_names(key):
    with pytest.raises(ValidationError):
        ProducerPlan.model_validate(plan_payload(task(key)))


@pytest.mark.parametrize("field", ["summary", "reason", "story"])
def test_plan_rejects_whitespace_only_business_text(field):
    payload = plan_payload()
    target = payload if field == "summary" else payload["tasks"][0]
    if field == "story":
        target = target["task"]
    target[field] = " \t\n "
    with pytest.raises(ValidationError):
        ProducerPlan.model_validate(payload)


@pytest.mark.parametrize("rounds", [0, 4, True, "1"])
@pytest.mark.parametrize("kind", ["screenwriter", "director"])
def test_specialist_rounds_cannot_exceed_or_coerce_limits(kind, rounds):
    payload = {"kind": kind}
    if kind == "screenwriter":
        payload.update(story="故事", revision_rounds=rounds)
    else:
        payload.update(script_version_id=SOURCE, director_revision_rounds=rounds)
    with pytest.raises(ValidationError):
        ProducerPlan.model_validate(plan_payload(task(payload=payload)))


def test_plan_requires_unique_real_tool_keys_and_valid_snapshot_hashes():
    payload = plan_payload()
    payload["evidence"].append(deepcopy(payload["evidence"][0]))
    with pytest.raises(ValidationError, match="evidence tool keys must be unique"):
        ProducerPlan.model_validate(payload)
    payload["evidence"][1]["evidence_sha256"] = "b" * 64
    with pytest.raises(ValidationError, match="evidence tool keys must be unique"):
        ProducerPlan.model_validate(payload)
    payload["evidence"][1]["tool_call_key"] = "budget"
    assert len(ProducerPlan.model_validate(payload).evidence) == 2
    for field in ("goal_sha256", "evidence_sha256"):
        bad = deepcopy(payload)
        target = bad if field == "goal_sha256" else bad["evidence"][0]
        target[field] = "A" * 64
        with pytest.raises(ValidationError):
            ProducerPlan.model_validate(bad)
    payload["evidence_invocation_ids"] = [SOURCE]
    with pytest.raises(ValidationError):
        ProducerPlan.model_validate(payload)


@pytest.mark.parametrize("field", ["tasks", "evidence"])
@pytest.mark.parametrize("count", [0, 17])
def test_plan_collections_have_nonzero_bounded_sizes(field, count):
    payload = plan_payload()
    if field == "tasks":
        payload[field] = [task(f"task-{i}") for i in range(count)]
    else:
        payload[field] = [
            {"tool_call_key": f"observe-{i}", "evidence_sha256": HASH} for i in range(count)
        ]
    with pytest.raises(ValidationError):
        ProducerPlan.model_validate(payload)


def test_plan_byte_limit_measures_utf8_and_accepts_exact_boundary():
    payload = ProducerPlan.model_validate(plan_payload(task("first"), task("second"))).model_dump(
        mode="json"
    )
    for item in payload["tasks"]:
        item["task"]["story"] = "x"
    overhead = len(json.dumps(payload, ensure_ascii=False).encode()) - 2
    payload["tasks"][0]["task"]["story"] = "x" * 20000
    payload["tasks"][1]["task"]["story"] = "x" * (32768 - overhead - 20000)
    assert len(json.dumps(payload, ensure_ascii=False).encode()) == 32768
    assert ProducerPlan.model_validate(payload)
    payload["tasks"][1]["task"]["story"] += "x"
    with pytest.raises(ValidationError, match="byte limit"):
        ProducerPlan.model_validate(payload)
    for item in payload["tasks"]:
        item["task"]["story"] = "故" * 6000
    assert len(json.dumps(payload, ensure_ascii=False)) < 32768
    with pytest.raises(ValidationError, match="byte limit"):
        ProducerPlan.model_validate(payload)


def test_goal_preserves_exact_source_and_rejects_blank_or_unstable_identity():
    original = "  精确目标\n保留换行  "
    assert ProducerGoal(goal=original, key="producer:episode.1-v1").goal == original
    assert WriterTask(kind="screenwriter", story=original).story == original
    for goal, key in [(" \n\t ", "ok"), (original, "../run"), (original, "x" * 101)]:
        with pytest.raises(ValidationError):
            ProducerGoal(goal=goal, key=key)


def test_delegation_receipt_retains_exact_plan_identity_without_authority_fields():
    request = {"plan_id": SOURCE, "plan_sha256": HASH, "task_key": "direct-1"}
    assert DelegationRequest.model_validate(request).plan_id == UUID(SOURCE)
    receipt = {
        **request,
        "delegation_id": OTHER,
        "child_run_id": THIRD,
        "root_run_id": SOURCE,
        "task_kind": "director",
    }
    result = DelegationResult.model_validate(receipt)
    assert result.version_no == 1
    reference = {"plan_id": SOURCE, "plan_sha256": HASH}
    assert ProducerPlanReference.model_validate(reference).version_no == 1
    for bad in [receipt | {"version_no": 2}, receipt | {"task_kind": "shell"}]:
        with pytest.raises(ValidationError):
            DelegationResult.model_validate(bad)
    for model, payload in [(DelegationRequest, request), (ProducerPlanReference, reference)]:
        with pytest.raises(ValidationError):
            model.model_validate(payload | {"limits": {"max_calls": 99999}})
    assert (WORKFLOW_VERSION, DELEGATE_TOOL) == ("producer/1", "delegate_production")
