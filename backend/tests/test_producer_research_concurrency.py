from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from uuid import UUID

from sqlalchemy import select
from test_concurrency import require_postgres
from test_generation_budget import set_policy
from test_producer_research import (
    observed,
    prepare_producer,
    producer_call,
    quoted_gateway_call,
)
from test_production_state import finish_text, read

from arclight.application import producer_research
from arclight.application.commands import require_project
from arclight.application.costs import read_costs, read_entries
from arclight.application.gateway_accounting import latest_receipt, reconcile
from arclight.domain.model_gateway import GatewayUsage
from arclight.persistence.generation_budget import GenerationBudgetPolicy
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import AssetVersion


def test_postgres_observation_cannot_mix_state_policy_and_bill_across_a_commit(
    client, episode, settings, monkeypatch
):
    require_postgres(settings)
    completed = finish_text(client, episode, settings)
    project_id = UUID(episode["project_id"])
    episode_id = UUID(episode["id"])
    script_id = UUID(read(client, episode)["script_version_id"])
    # The real text workflow records explicit zero-cost Mock gateway receipts.
    # They are confirmed bills, separate from the unconfirmed quote added below.
    with client.app.state.session_factory() as db:
        text_calls = list(
            db.scalars(select(GatewayCall).where(GatewayCall.project_id == project_id))
        )
        assert text_calls
        free_call_ids = {call.id for call in text_calls}
        free_receipt_ids = {}
        for call in text_calls:
            assert call.workflow_id == UUID(completed["id"])
            assert call.episode_id == episode_id and call.generation_attempt_id is None
            assert call.route_json["provider"] == "mock"
            assert call.maximum_cost == Decimal("0")
            receipt = latest_receipt(db, call.id)
            assert receipt is not None and receipt.status == "SUCCEEDED"
            assert receipt.actual_cost == Decimal("0")
            free_receipt_ids[call.id] = receipt.id
    policy = set_policy(client, episode["project_id"], "100", task_limit="20")
    call_id = quoted_gateway_call(client, episode)
    registry, run_id, actor = prepare_producer(client, episode, settings)
    with client.app.state.session_factory() as db:
        quoted = db.get(GatewayCall, call_id)
        assert quoted.id not in free_call_ids and quoted.maximum_cost == Decimal("2")
        quoted_receipt = latest_receipt(db, call_id)
        assert quoted_receipt is not None and quoted_receipt.actual_cost is None
        original_receipt_id = quoted_receipt.id
        call_ids = set(
            db.scalars(select(GatewayCall.id).where(GatewayCall.project_id == project_id))
        )
        assert call_ids == free_call_ids | {call_id}
        quoted_entries = read_entries(db, project_id, actor, task_id=call_id)
        assert quoted_entries.total == 1
        assert quoted_entries.items[0].call_id == call_id
        assert quoted_entries.items[0].actual_cost is None
        assert quoted_entries.items[0].estimated_cost == Decimal("2")
        assert quoted_entries.items[0].pending_estimate == Decimal("2")
        independent_before = {
            "project_costs": read_costs(db, project_id, actor).summary.model_dump(mode="json"),
            "episode_costs": read_costs(
                db, project_id, actor, episode_id=episode_id
            ).summary.model_dump(mode="json"),
        }
    expected_before = {
        "entry_count": len(free_call_ids) + 1,
        "confirmed_count": len(free_call_ids),
        "estimated_count": 1,
        "unpriced_count": 0,
        "actual_cost": "0.000000",
        "estimated_cost": "2.000000",
        "pending_estimate": "2.000000",
        "projected_cost": "2.000000",
        "cost_is_estimate": True,
    }
    before = observed(client, registry, producer_call(run_id, key="before"))
    for name in ("project_costs", "episode_costs"):
        assert independent_before[name] == expected_before
        assert before["budget"][name] == independent_before[name]
    attempted, committed = Event(), Event()
    original = producer_research.read_production_state
    pending = []

    def write():
        with client.app.state.session_factory.begin() as db:
            # Preserve the domain's project-before-policy/version/receipt lock order.
            attempted.set()
            project = require_project(
                db, UUID(episode["project_id"]), actor, write=True, owner=True
            )
            project.budget_limit = Decimal("50")
            current_policy = db.get(GenerationBudgetPolicy, project.id)
            current_policy.revision += 1
            current_policy.task_limit = Decimal("15")
            db.get(AssetVersion, script_id).stale = True
            reconcile(
                db,
                db.get(GatewayCall, call_id),
                actor,
                "SUCCEEDED",
                GatewayUsage(actual_cost=Decimal("0.3")),
                "private-concurrent-invoice",
            )
        committed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def during_read(db, *args, **kwargs):
            if not pending:
                pending.append(pool.submit(write))
                assert attempted.wait(10)
                assert not committed.wait(0.3)
            return original(db, *args, **kwargs)

        monkeypatch.setattr(producer_research, "read_production_state", during_read)
        during = observed(client, registry, producer_call(run_id, key="during"))
        pending[0].result(timeout=10)
    monkeypatch.setattr(producer_research, "read_production_state", original)
    after = observed(client, registry, producer_call(run_id, key="after"))

    assert during["evidence_sha256"] == before["evidence_sha256"]
    assert "STALE_INPUT" not in during["production"]["issues"]
    assert during["budget"]["project_limit"] == "100.000000"
    assert during["budget"]["policy_revision"] == policy["policy_revision"]
    assert during["budget"]["task_limit"] == "20.000000"
    for name in ("project_costs", "episode_costs"):
        assert during["budget"][name] == before["budget"][name] == expected_before
    assert after["evidence_sha256"] != before["evidence_sha256"]
    assert "STALE_INPUT" in after["production"]["issues"]
    assert after["budget"]["project_limit"] == "50.000000"
    assert after["budget"]["policy_revision"] == policy["policy_revision"] + 1
    assert after["budget"]["task_limit"] == "15.000000"
    expected_after = {
        **expected_before,
        "confirmed_count": len(free_call_ids) + 1,
        "estimated_count": 0,
        "actual_cost": "0.300000",
        "pending_estimate": "0.000000",
        "projected_cost": "0.300000",
        "cost_is_estimate": False,
    }
    with client.app.state.session_factory() as db:
        independent_after = {
            "project_costs": read_costs(db, project_id, actor).summary.model_dump(mode="json"),
            "episode_costs": read_costs(
                db, project_id, actor, episode_id=episode_id
            ).summary.model_dump(mode="json"),
        }
        assert (
            set(db.scalars(select(GatewayCall.id).where(GatewayCall.project_id == project_id)))
            == call_ids
        )
        assert db.get(GatewayReceipt, original_receipt_id).actual_cost is None
        assert latest_receipt(db, call_id).actual_cost == Decimal("0.3")
        assert {
            identity: latest_receipt(db, identity).id for identity in free_call_ids
        } == free_receipt_ids
        assert all(
            latest_receipt(db, identity).actual_cost == Decimal("0") for identity in free_call_ids
        )
    for name in ("project_costs", "episode_costs"):
        assert after["budget"][name] == independent_after[name] == expected_after
        assert after["budget"][name]["entry_count"] == before["budget"][name]["entry_count"]
    assert "private-concurrent-invoice" not in str(after)
