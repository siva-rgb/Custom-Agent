"""M17 gate: the budget governor (FR-67, FR-68, FR-69, AC-53, AC-54, AC-55, NFR-23).

Written before the implementation, against the approved specification with FR-67 to
FR-69 as clarified on 2026-09-19 by DECISION-c274eb02 and DECISION-727f3a42: M17 builds
a governor over a PlanVersion plus an internal lease the agent loop checks before each
model call, because nothing runs plan nodes or child runs until M18 and M19 and budgets
for plain single-agent runs stay out of scope (I-04); and the shipped price table holds
openai.gpt-4o-mini alone, cited and dated.

The surface these tests pin, in `agentsdk.budget` and exported from `agentsdk`:
  * `BudgetPolicy`: frozen, keyword-only, validated at construction by field name, with
    `run_ceiling_usd`, `run_ceiling_tokens` (at least one), and the P2-D19 defaults
    `orchestrator_reserve_fraction` 0.20, `unallocated_reserve_fraction` 0.20,
    `reservation_cap_fraction` 0.40, plus `max_replans`.
  * `BudgetAmount(usd=None, tokens=None)`: a non-negative amount in either unit.
  * `BudgetGovernor(policy, plan)`: `.allocation` (orchestrator, unallocated,
    reservations by node), `.lease(node_id)`, `.orchestrator_lease()`, `.spend`,
    `.remaining_unallocated`, `.top_up(node_id, amount)` and `.may_start_child()`.
  * `BudgetLease`: `.node_id`, `.reservation`, `.spent`, `.may_call()`,
    `.charge(usage, cost)` and `.release()`, which returns the unspent remainder to the
    run pool.
  * `RunConfig(budget_lease=...)`: set by the orchestrator M19 brings, not by
    application code; a run refused a call ends FAILED with `error == "budget_exceeded"`.
  * `agentsdk.prices`: `PRICE_TABLE`, `PRICE_TABLE_DATE`, `PRICE_TABLE_SOURCES` and
    `shipped_pricing(model_id)`, the default behind a caller's ModelPricing.
  * `UnpricedModel`, an `AgentSDKError` and a `ValueError`: a USD ceiling on a model the
    effective pricing does not cover, raised at the call site and naming the model.

The persisted half needs DATABASE_URL and fails rather than skips without it. Every row
written is removed, and no assertion prints a credential. Names M17 adds are reached
through their modules at call time, so before the implementation each test fails on its
own.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import uuid
from decimal import Decimal

import psycopg
import pytest
from dotenv import load_dotenv

import agentsdk
from agentsdk import AgentSpec, Persistence, RunConfig, Runner, RunStatus, migrate
from agentsdk.config import normalise_database_url
from agentsdk.errors import AgentSDKError
from agentsdk.migrate import apply_migrations
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import SCHEMA_PATH
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
from agentsdk.session import InMemorySessionStore

load_dotenv()


DSN = normalise_database_url(os.environ.get("DATABASE_URL"))


REPO = pathlib.Path(__file__).resolve().parents[1]
TENANT, PROJECT = "SYN-m17", "p-m17"


MODEL = "priced-model"
# One call: 10 prompt tokens at 0.01 and 1 completion token at 0.10 is 0.20 USD.


PRICING = ModelPricing(input=Decimal("0.01"), output=Decimal("0.10"))


CALL_USD = Decimal("0.20")


CALL_TOKENS = 11


POLICY_FIELDS = [
    "run_ceiling_usd", "run_ceiling_tokens", "orchestrator_reserve_fraction",
    "unallocated_reserve_fraction", "reservation_cap_fraction", "max_replans",
]


# --- shared helpers --------------------------------------------------------------------------------


def budget():
    return importlib.import_module("agentsdk.budget")


def prices():
    return importlib.import_module("agentsdk.prices")


def plan_module():
    return importlib.import_module("agentsdk.plan")


def policy(**overrides):
    fields = dict(run_ceiling_usd=Decimal("10.00"))
    fields.update(overrides)
    return budget().BudgetPolicy(**fields)


def a_plan(run_id, proposals):
    """A plan whose nodes propose the reservations given, None meaning no proposal."""
    p = plan_module()
    nodes = tuple(
        p.PlanNode(
            node_id=node_id,
            objective=f"do {node_id}",
            assigned_role="worker",
            budget_reservation=None if usd is None else p.BudgetReservation(usd=Decimal(usd)),
        )
        for node_id, usd in proposals.items()
    )
    return p.PlanVersion(plan_id=str(uuid.uuid4()), version=1, run_id=run_id, nodes=nodes)


def governor(proposals, **policy_overrides):
    return budget().BudgetGovernor(policy(**policy_overrides), a_plan(str(uuid.uuid4()), proposals))


def amounts(allocation):
    """{node_id: usd} for a governor's reservations, as plain Decimals."""
    return {node_id: amount.usd for node_id, amount in allocation.reservations.items()}


class ScriptedModel:
    """Answers every turn, costing exactly CALL_USD under PRICING."""

    def __init__(self, calls=None):
        self.calls = 0
        self.limit = calls

    async def send(self, request):
        self.calls += 1
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="done"),
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 1, 11),
        )


def registry(pricing=PRICING, model_id=MODEL):
    return ModelRegistry([
        ModelEntry(
            provider="test", model_id=model_id, model_version="1", adapter_version="test/1",
            capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=pricing),
        )
    ])


def runner(model=None, pricing=PRICING, persistence=None, model_id=MODEL):
    return Runner(
        {"scripted": model or ScriptedModel()},
        session_store=InMemorySessionStore(),
        model_registry=registry(pricing, model_id),
        persistence=persistence,
    )


AGENT = AgentSpec(id="spender", instructions="Reply in one word.")


def run_with(lease, *, model=None, persistence=None, pricing=PRICING, max_turns=1, model_id=MODEL):
    config = RunConfig(
        tenant_id=TENANT, project_id=PROJECT, max_turns=max_turns, model_override=model_id,
        budget_lease=lease,
    )
    return asyncio.run(runner(model, pricing, persistence, model_id).run(AGENT, "spend", config))


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


def remove_rows():
    with psycopg.connect(DSN, autocommit=True) as conn:
        ids = [row[0] for row in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE 'SYN-m17%%'").fetchall()]
        for table in ("plan_node_states", "plan_versions"):
            if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None:
                conn.execute(f"DELETE FROM {table} WHERE tenant_id LIKE 'SYN-m17%%'")
        for table in ("run_events", "messages", "execution_manifests"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


@pytest.fixture(autouse=True)


def remove_what_the_test_wrote():
    yield
    if DSN:
        remove_rows()


# =================================================================================================
# FR-67: the policy and the plan-time split
# =================================================================================================


def test_the_new_public_names_exist():
    for name in ("BudgetPolicy", "BudgetAmount", "BudgetGovernor", "BudgetLease", "UnpricedModel"):
        assert name in agentsdk.__all__ and hasattr(agentsdk, name), name
    errors = importlib.import_module("agentsdk.errors")
    assert issubclass(errors.UnpricedModel, AgentSDKError) and issubclass(errors.UnpricedModel, ValueError)
    assert issubclass(errors.BudgetExceeded, errors.WorkflowError)
    kind = budget().BudgetPolicy
    assert dataclasses.is_dataclass(kind) and kind.__dataclass_params__.frozen
    assert [f.name for f in dataclasses.fields(kind)] == POLICY_FIELDS


def test_the_policy_defaults_are_the_owners_figures():
    made = policy()
    assert made.orchestrator_reserve_fraction == Decimal("0.20")
    assert made.unallocated_reserve_fraction == Decimal("0.20")
    assert made.reservation_cap_fraction == Decimal("0.40")
    assert made.run_ceiling_tokens is None
    assert isinstance(made.max_replans, int) and made.max_replans >= 0


BAD_POLICIES = [
    dict(run_ceiling_usd=None),  # neither ceiling
    dict(run_ceiling_usd=Decimal("0")), dict(run_ceiling_usd=Decimal("-1")),
    dict(run_ceiling_usd=0.5), dict(run_ceiling_usd="10"), dict(run_ceiling_usd=Decimal("NaN")),
    dict(run_ceiling_usd=None, run_ceiling_tokens=0), dict(run_ceiling_usd=None, run_ceiling_tokens=True),
    dict(orchestrator_reserve_fraction=Decimal("0")), dict(orchestrator_reserve_fraction=Decimal("1")),
    dict(orchestrator_reserve_fraction=0.2), dict(unallocated_reserve_fraction=Decimal("-0.1")),
    dict(reservation_cap_fraction=Decimal("0")), dict(reservation_cap_fraction=Decimal("1.5")),
    # The two reserves must leave something for the nodes.
    dict(orchestrator_reserve_fraction=Decimal("0.60"), unallocated_reserve_fraction=Decimal("0.40")),
    dict(max_replans=-1), dict(max_replans=True), dict(max_replans="3"),
]


@pytest.mark.parametrize("overrides", BAD_POLICIES, ids=range(len(BAD_POLICIES)))
def test_every_invalid_policy_is_refused_by_field_name(overrides):
    with pytest.raises(ValueError) as refused:
        policy(**overrides)
    named = [f for f in POLICY_FIELDS if f in str(refused.value)]
    assert named, f"the refusal names no field: {refused.value}"


def test_the_plan_time_split_follows_ac53():
    """AC-53: 20% and 20% reserved, a greedy proposal capped at 40% of what is
    unallocated, and the nodes that propose nothing splitting the rest equally."""
    # Ceiling 10.00: orchestrator 2.00, unallocated 2.00, leaving 6.00 for the nodes.
    # "greedy" proposes 5.00, above 40% of 6.00, so it is capped to 2.40.
    # "modest" proposes 1.20 and keeps it. 2.40 remains for the two that propose
    # nothing, 1.20 each.
    made = governor({"greedy": "5.00", "modest": "1.20", "quiet": None, "silent": None})
    allocation = made.allocation
    assert allocation.orchestrator.usd == Decimal("2.00")
    assert allocation.unallocated.usd == Decimal("2.00")
    assert amounts(allocation) == {
        "greedy": Decimal("2.40"), "modest": Decimal("1.20"),
        "quiet": Decimal("1.20"), "silent": Decimal("1.20"),
    }
    assert sum(amounts(allocation).values()) + allocation.orchestrator.usd + allocation.unallocated.usd == Decimal("10.00")


def test_a_node_that_ends_under_its_reservation_returns_the_difference():
    made = governor({"a": "3.00", "b": None})
    lease = made.lease("a")
    before = made.remaining_unallocated.usd
    reserved = lease.reservation.usd  # read before the release, which gives back the rest
    lease.charge(Usage(10, 1, 11), CALL_USD)
    lease.release()
    assert made.spend.usd == CALL_USD
    assert made.remaining_unallocated.usd == before + (reserved - CALL_USD)
    assert made.lease("b").reservation.usd == amounts(made.allocation)["b"], "another node keeps its own"


def test_a_top_up_comes_only_from_the_unallocated_reserve():
    made = governor({"a": "1.00", "b": None})
    reserve = made.remaining_unallocated.usd
    made.top_up("a", budget().BudgetAmount(usd=Decimal("0.50")))
    assert made.lease("a").reservation.usd == Decimal("1.50")
    assert made.remaining_unallocated.usd == reserve - Decimal("0.50")
    with pytest.raises(importlib.import_module("agentsdk.errors").BudgetExceeded):
        made.top_up("a", budget().BudgetAmount(usd=reserve))
    with pytest.raises(KeyError):
        made.top_up("ghost", budget().BudgetAmount(usd=Decimal("0.01")))


def test_a_token_ceiling_is_split_the_same_way():
    made = governor({"a": None, "b": None}, run_ceiling_usd=None, run_ceiling_tokens=1000)
    allocation = made.allocation
    assert (allocation.orchestrator.tokens, allocation.unallocated.tokens) == (200, 200)
    assert [amount.tokens for amount in allocation.reservations.values()] == [300, 300]
    assert all(amount.usd is None for amount in allocation.reservations.values())


# =================================================================================================
# FR-68: soft enforcement, and what it does to a run
# =================================================================================================


def test_a_lease_stops_at_its_reservation_after_at_most_one_overshoot():
    made = governor({"a": "0.30", "b": None})
    lease = made.lease("a")
    assert lease.may_call()
    lease.charge(Usage(10, 1, 11), CALL_USD)  # 0.20 spent of 0.30
    assert lease.may_call(), "under its reservation, so one more call is allowed"
    lease.charge(Usage(10, 1, 11), CALL_USD)  # 0.40 spent: over by one call
    assert not lease.may_call()
    assert lease.spent.usd == Decimal("0.40") and lease.reservation.usd == Decimal("0.30")
    assert made.spend.usd == Decimal("0.40"), "the overshoot is charged to the run"


def test_a_released_lease_makes_no_further_call():
    """Its remainder has gone back to the run pool, so spending it again would spend
    money the governor has already promised elsewhere."""
    made = governor({"a": "5.00", "b": None})
    lease = made.lease("a")
    assert lease.may_call()
    lease.release()
    assert not lease.may_call() and lease.released
    result = run_with(lease, model=(model := ScriptedModel()))
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert model.calls == 0


def test_a_token_reservation_stops_a_lease_just_as_a_usd_one_does():
    made = governor({"a": None, "b": None}, run_ceiling_usd=None, run_ceiling_tokens=100)
    lease = made.lease("a")
    assert lease.reservation.tokens == 30 and lease.reservation.usd is None
    assert lease.may_call()
    lease.charge(Usage(25, 5, 30), None)
    assert not lease.may_call(), "at its token reservation, it makes no further call"
    assert lease.spent.tokens == 30
    result = run_with(lease, model=(model := ScriptedModel()), pricing=None)
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert model.calls == 0


def test_a_run_refused_a_call_ends_failed_with_budget_exceeded():
    made = governor({"a": "0.10"})
    lease = made.lease("a")
    lease.charge(Usage(10, 1, 11), CALL_USD)  # already over
    model = ScriptedModel()
    result = run_with(lease, model=model)
    assert result.status is RunStatus.FAILED
    assert result.error == "budget_exceeded", result.error
    assert model.calls == 0, "no model call was made"


def test_a_run_inside_its_reservation_completes_and_charges_the_lease():
    made = governor({"a": "5.00"})
    lease = made.lease("a")
    result = run_with(lease)
    assert result.status is RunStatus.COMPLETED
    assert result.cost_usd == CALL_USD
    assert lease.spent.usd == CALL_USD and made.spend.usd == CALL_USD


def test_once_the_run_ceiling_is_reached_no_call_and_no_child_starts():
    made = governor({"a": "9.00", "b": None}, run_ceiling_usd=Decimal("1.00"))
    lease = made.lease("a")
    for _ in range(6):  # spend past the whole ceiling
        lease.charge(Usage(10, 1, 11), CALL_USD)
    assert made.spend.usd >= Decimal("1.00")
    assert not made.may_start_child()
    assert not made.lease("b").may_call(), "the run's ceiling stops a node inside its own reservation"


def test_the_overshoot_is_bounded_by_the_agents_running_at_once():
    """AC-54: four agents at once can overshoot the ceiling by at most one call each."""
    made = governor({f"n{i}": None for i in range(4)}, run_ceiling_usd=Decimal("1.00"))
    leases = [made.lease(f"n{i}") for i in range(4)]
    allowed = [lease for lease in leases if lease.may_call()]
    for lease in allowed:
        lease.charge(Usage(10, 1, 11), CALL_USD)
    while any(lease.may_call() for lease in leases):
        for lease in leases:
            if lease.may_call():
                lease.charge(Usage(10, 1, 11), CALL_USD)
    assert made.spend.usd <= Decimal("1.00") + len(leases) * CALL_USD
    # Each stopped at its own reservation, so the run never reached its ceiling: the
    # bound holds from the leases alone, which is what makes the overshoot bounded.
    assert all(not lease.may_call() for lease in leases)
    assert all(lease.spent.usd - lease.reservation.usd <= CALL_USD for lease in leases)


def test_an_unknown_cost_leaves_the_spend_unknown_rather_than_zero():
    """NFR-11 and NFR-23: a call nothing could price makes the spend None, not 0."""
    made = governor({"a": "1.00"}, run_ceiling_usd=None, run_ceiling_tokens=1000)
    lease = made.lease("a")
    lease.charge(Usage(10, 1, 11), None)
    assert lease.spent.usd is None and made.spend.usd is None
    assert lease.spent.tokens == CALL_TOKENS and made.spend.tokens == CALL_TOKENS
    assert lease.may_call(), "a token ceiling still governs, and it is not reached"


# =================================================================================================
# FR-69: the shipped price table
# =================================================================================================


def test_the_price_table_ships_dated_cited_and_holding_the_models_in_use():
    table = prices()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", table.PRICE_TABLE_DATE), table.PRICE_TABLE_DATE
    assert set(table.PRICE_TABLE) == {"openai.gpt-4o-mini"}, "DECISION-727f3a42"
    for model_id in table.PRICE_TABLE:
        assert table.PRICE_TABLE_SOURCES[model_id].startswith("https://"), model_id
    priced = table.shipped_pricing("openai.gpt-4o-mini")
    assert (priced.input, priced.cache_read, priced.output) == (
        Decimal("0.00000015"), Decimal("0.000000075"), Decimal("0.0000006"),
    )
    assert table.shipped_pricing("bedrock.anthropic.claude-haiku-4-5") is None, "left out until verified"
    assert table.shipped_pricing("no-such-model") is None
    path = pathlib.Path(table.PRICE_TABLE_PATH)
    assert path.is_file() and path.resolve().is_relative_to(pathlib.Path(agentsdk.__file__).resolve().parent)
    patterns = re.search(r"agentsdk = \[([^\]]*)\]", (REPO / "pyproject.toml").read_text(encoding="utf-8")).group(1)
    relative = path.resolve().relative_to(pathlib.Path(agentsdk.__file__).resolve().parent).as_posix()
    assert any(pathlib.PurePosixPath(relative).match(p.strip().strip('"')) for p in patterns.split(",")), relative


def test_a_callers_pricing_overrides_the_shipped_table():
    model = ScriptedModel()
    result = asyncio.run(
        runner(model, PRICING, None, "openai.gpt-4o-mini").run(
            AGENT, "spend",
            RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=1, model_override="openai.gpt-4o-mini"),
        )
    )
    assert result.cost_usd == CALL_USD, "the registry's price won, not the table's"


def test_the_shipped_table_prices_a_model_the_caller_left_unpriced():
    model = ScriptedModel()
    result = asyncio.run(
        runner(model, None, None, "openai.gpt-4o-mini").run(
            AGENT, "spend",
            RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=1, model_override="openai.gpt-4o-mini"),
        )
    )
    expected = Decimal("10") * Decimal("0.00000015") + Decimal("1") * Decimal("0.0000006")
    assert result.cost_usd == expected


def test_a_usd_ceiling_on_an_unpriced_model_is_a_configuration_error_naming_it():
    """AC-55, and the token ceiling beside it still completes."""
    made = governor({"a": "1.00"})
    with pytest.raises(importlib.import_module("agentsdk.errors").UnpricedModel, match="unpriced-model"):
        run_with(made.lease("a"), pricing=None, model_id="unpriced-model")
    tokens_only = governor({"a": "1.00"}, run_ceiling_usd=None, run_ceiling_tokens=1000)
    result = run_with(tokens_only.lease("a"), pricing=None, model_id="unpriced-model")
    assert result.status is RunStatus.COMPLETED and result.cost_usd is None
    assert tokens_only.spend.tokens == CALL_TOKENS


# =================================================================================================
# FR-68, FR-69, NFR-23: what the manifest records, on Postgres
# =================================================================================================


def test_a_failing_terminal_write_is_reported_without_a_second_terminal_event():
    """J1 (round 4, rejected): a terminal store write that fails is still reported to
    the caller, because a run recorded as still running is a lie the caller should hear
    about (M4's contract, test_a_persistence_failure_on_the_success_path_is_not_swallowed).
    What must NOT happen is a second terminal event written over the first: round 4 found
    RunCompleted followed by RunFailed on one run."""
    class Exploding:
        records_accounting = True
        records_budget = True

        def __init__(self, inner):
            self.inner = inner
            self.attempts = 0

        def start_run(self, *args, **kwargs):
            return self.inner.start_run(*args, **kwargs)

        def finish_run(self, *args, **kwargs):
            self.attempts += 1
            raise RuntimeError("the terminal write failed")

    assert DSN, "DATABASE_URL must be set"
    persistence = Persistence.postgres(DSN)
    exploding = Exploding(persistence.runs)
    persistence = dataclasses.replace(persistence, runs=exploding)
    made = governor({"a": "5.00"})
    result = run_with(made.lease("a"), persistence=persistence)

    assert result.status is RunStatus.FAILED, "the caller hears that the run was not recorded"
    assert "the terminal write failed" in (result.error or "")
    terminal = [e.event_type.value for e in result.events
                if e.event_type.value in ("RunCompleted", "RunFailed", "RunCancelled")]
    assert terminal == ["RunCompleted"], terminal
    stored = [
        row[0] for row in query(
            "SELECT event_type FROM run_events WHERE run_id = %s"
            " AND event_type IN ('RunCompleted','RunFailed','RunCancelled')", (result.run_id,),
        )
    ]
    assert stored == ["RunCompleted"], stored
    assert exploding.attempts == 1


def test_a_budget_column_that_cannot_be_written_still_leaves_a_terminal_row():
    """J1's other half: the budget columns are accounting, and accounting never fails a
    run (NFR-11). A spend the database cannot hold must not cost the run its status."""
    assert DSN, "DATABASE_URL must be set"
    stores = importlib.import_module("agentsdk.postgres")
    persistence = Persistence.postgres(DSN)
    scope = stores.RunScope(run_id=str(uuid.uuid4()), tenant_id=TENANT, project_id=PROJECT)
    manifest = importlib.import_module("agentsdk.manifest").build_manifest(
        sdk_version="m17", agent_spec_id="m17", instructions="i", tool_profile=(),
        tool_spec_hashes=[], model_id=None,
    )
    persistence.runs.start_run(
        scope, agent_spec_id="m17", max_turns=1, model_id=None, principal_context=None, manifest=manifest,
    )
    # A value JSONB cannot hold: the status write must survive it.
    persistence.runs.finish_run(
        scope, "completed", usage=Usage(1, 1, 2), cost_usd=Decimal("0.01"),
        budget_spend={"usd": object()}, price_table_date="2026-09-19",
    )
    status, = query("SELECT status FROM runs WHERE run_id = %s", (scope.run_id,))[0]
    assert status == "completed"
    spend, = query("SELECT budget_spend FROM execution_manifests WHERE run_id = %s", (scope.run_id,))[0]
    assert spend is None, spend


def test_the_manifest_records_the_policy_the_reservations_the_spend_and_the_table_date():
    assert DSN, "DATABASE_URL must be set"
    persistence = Persistence.postgres(DSN)
    made = governor({"a": "5.00", "b": None})
    lease = made.lease("a")
    result = run_with(lease, persistence=persistence)
    assert result.status is RunStatus.COMPLETED
    row = query(
        "SELECT budget_policy, budget_reservations, budget_spend, price_table_date"
        " FROM execution_manifests WHERE run_id = %s", (result.run_id,),
    )
    assert len(row) == 1
    stored_policy, reservations, spend, table_date = row[0]
    assert Decimal(stored_policy["run_ceiling_usd"]) == Decimal("10.00")
    assert Decimal(stored_policy["reservation_cap_fraction"]) == Decimal("0.40")
    assert Decimal(reservations["a"]["usd"]) == lease.reservation.usd
    assert Decimal(spend["usd"]) == CALL_USD
    assert spend["node_id"] == "a" and Decimal(spend["node_total"]["usd"]) == lease.spent.usd
    assert table_date is None, "the caller priced this model, so no shipped price was used"

    priced_by_table = run_with(
        governor({"a": "5.00"}).lease("a"), persistence=persistence, pricing=None, model_id="openai.gpt-4o-mini",
    )
    date_row = query("SELECT price_table_date FROM execution_manifests WHERE run_id = %s", (priced_by_table.run_id,))
    assert date_row[0][0] == prices().PRICE_TABLE_DATE


def test_the_manifest_records_what_each_run_spent_not_the_leases_running_total():
    """F1 (round 1, rejected): budget_spend is this run's own spend. A second run on
    the same lease, and a run refused before its first call, must not inherit what an
    earlier run spent -- FR-68's final spend, and NFR-23's sum of ModelCalled costs."""
    assert DSN, "DATABASE_URL must be set"
    persistence = Persistence.postgres(DSN)
    made = governor({"a": "0.30"})
    lease = made.lease("a")

    first = run_with(lease, persistence=persistence)
    second = run_with(lease, persistence=persistence)   # same lease, one more call
    refused = run_with(lease, persistence=persistence)  # over its reservation: no call at all
    assert (first.status, second.status) == (RunStatus.COMPLETED, RunStatus.COMPLETED)
    assert refused.status is RunStatus.FAILED and refused.error == "budget_exceeded"

    # The node's running total at the moment each run ended: it grows, while each
    # run's own spend does not inherit the one before it.
    for result, expected_usd, expected_calls, expected_total in (
        (first, CALL_USD, 1, CALL_USD),
        (second, CALL_USD, 1, CALL_USD * 2),
        (refused, Decimal(0), 0, CALL_USD * 2),
    ):
        spend, = query(
            "SELECT budget_spend FROM execution_manifests WHERE run_id = %s", (result.run_id,),
        )[0]
        stored_cost = query("SELECT cost_usd FROM runs WHERE run_id = %s", (result.run_id,))[0][0]
        calls = query(
            "SELECT count(*) FROM run_events WHERE run_id = %s AND event_type = 'ModelCalled'", (result.run_id,),
        )[0][0]
        assert calls == expected_calls, (result.run_id, calls)
        assert Decimal(spend["usd"]) == expected_usd == Decimal(stored_cost), (result.run_id, spend, stored_cost)
        # The node's running total is still recorded, under its own key.
        assert Decimal(spend["node_total"]["usd"]) == expected_total, (result.run_id, spend)


def test_a_cancelled_call_leaves_the_governors_spend_unknown_rather_than_definite():
    """F2: a call cancelled in flight may already be billed and reports no usage
    (P2-D7), so the lease's USD spend becomes unknown, exactly as the run's cost does."""
    made = governor({"a": "5.00"})
    lease = made.lease("a")
    started, release = asyncio.Event(), asyncio.Event()

    class BlockingModel:
        calls = 0

        async def send(self, request):
            type(self).calls += 1
            started.set()
            await release.wait()
            raise AssertionError("the call should have been cancelled")

    async def go():
        handle = await runner(BlockingModel(), PRICING, None).start(
            AGENT, "spend",
            RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=1, model_override=MODEL, budget_lease=lease),
        )
        await started.wait()
        handle.cancel("stopping")
        try:
            await handle.result()
        except asyncio.CancelledError:
            pass
        release.set()

    asyncio.run(go())
    assert lease.spent.usd is None, "a call that may have been billed leaves the spend unknown"
    assert made.spend.usd is None
    assert not lease.may_call(), "and the run cannot be shown to be inside its ceiling"


def test_a_lease_charges_tokens_a_provider_reports_without_a_total():
    """F5: Usage that carries prompt and completion but no total still costs tokens."""
    made = governor({"a": None}, run_ceiling_usd=None, run_ceiling_tokens=1000)
    lease = made.lease("a")
    lease.charge(Usage(prompt_tokens=7, completion_tokens=3), None)
    assert lease.spent.tokens == 10 and made.spend.tokens == 10


def test_a_top_up_is_refused_whole_or_applied_whole():
    """F3 and F4: a released lease cannot be topped up, and a top-up that cannot be
    paid in one unit moves nothing in the other."""
    errors = importlib.import_module("agentsdk.errors")
    made = governor({"a": None, "b": None}, run_ceiling_usd=Decimal("10.00"), run_ceiling_tokens=1000)
    reserve_before = made.remaining_unallocated
    with pytest.raises(errors.BudgetExceeded):
        made.top_up("a", budget().BudgetAmount(usd=Decimal("0.10"), tokens=10_000))
    assert made.remaining_unallocated == reserve_before, "the USD half must not have moved"

    lease = made.lease("b")
    lease.release()
    after_release = made.remaining_unallocated.usd  # its unspent reservation came back
    with pytest.raises(ValueError, match="b"):
        made.top_up("b", budget().BudgetAmount(usd=Decimal("0.10")))
    assert made.remaining_unallocated.usd == after_release


@pytest.mark.parametrize("ceiling", ["10.00", "1.00", "0.07", "3.33"])
@pytest.mark.parametrize("silent", [3, 7])
def test_the_split_always_sums_to_the_ceiling(ceiling, silent):
    """F6: an equal share that does not terminate must not lose or invent money.
    Seven silent nodes make the share recur, where three divide exactly."""
    made = governor({chr(ord("a") + i): None for i in range(silent)}, run_ceiling_usd=Decimal(ceiling))
    allocation = made.allocation
    total = allocation.orchestrator.usd + allocation.unallocated.usd + sum(amounts(allocation).values())
    assert total == Decimal(ceiling), (total, ceiling)
    shares = set(amounts(allocation).values())
    assert len(shares) == 1, f"the silent nodes must share equally: {shares}"


def test_every_node_proposing_leaves_the_remainder_unallocated():
    """F9: the case where no node is silent, so nothing is split equally."""
    made = governor({"a": "0.50", "b": "0.50"})
    allocation = made.allocation
    assert amounts(allocation) == {"a": Decimal("0.50"), "b": Decimal("0.50")}
    total = allocation.orchestrator.usd + allocation.unallocated.usd + sum(amounts(allocation).values())
    assert total == Decimal("10.00")
    assert allocation.unallocated.usd == Decimal("10.00") - Decimal("2.00") - Decimal("1.00")


def test_a_lease_exactly_at_its_reservation_makes_no_further_call():
    """F9: the boundary itself, where >= and > differ."""
    made = governor({"a": "0.40", "b": None})
    lease = made.lease("a")
    lease.charge(Usage(10, 1, 11), Decimal("0.40"))
    assert lease.spent.usd == lease.reservation.usd
    assert not lease.may_call()


def test_a_run_exactly_at_its_ceiling_starts_no_call_and_no_child():
    """F9: the same boundary on the run's ceiling."""
    made = governor({"a": None}, run_ceiling_usd=Decimal("1.00"))
    lease = made.lease("a")
    lease.charge(Usage(10, 1, 11), Decimal("1.00"))
    assert made.spend.usd == made.policy.run_ceiling_usd
    assert not made.may_start_child() and not lease.may_call()


def test_a_reclaim_never_returns_more_than_the_node_held():
    """F9: a node that overshot returns nothing, never a negative amount."""
    made = governor({"a": "0.30", "b": None})
    lease = made.lease("a")
    lease.charge(Usage(10, 1, 11), Decimal("0.50"))  # past its reservation
    before = made.remaining_unallocated.usd
    lease.release()
    assert made.remaining_unallocated.usd == before, "an overspent node reclaims nothing"


def test_an_unpriced_call_under_a_usd_ceiling_stops_the_run_at_the_next_call():
    """F7: a before_model hook can send a model the USD ceiling's check never saw. The
    call is not stopped, but its unknown cost makes the spend unknowable, so the next
    call is refused rather than spent blind."""
    made = governor({"a": "5.00"})
    lease = made.lease("a")
    lease.charge(Usage(10, 1, 11), None)  # a call nothing could price
    assert lease.spent.usd is None and made.spend.usd is None
    assert not lease.may_call()
    result = run_with(lease, model=(model := ScriptedModel()))
    assert result.status is RunStatus.FAILED and result.error == "budget_exceeded"
    assert model.calls == 0


class NoTotalModel:
    """A provider that reports the parts and no total, which the OpenAI-compatible
    adapter passes straight through (openai_compatible.py, usage.get("total_tokens"))."""

    def __init__(self):
        self.calls = 0

    async def send(self, request):
        self.calls += 1
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="done"),
            stop_reason=StopReason.END_TURN,
            usage=Usage(prompt_tokens=10, completion_tokens=1),
        )


def test_the_manifest_counts_tokens_a_provider_reported_without_a_total():
    """G1 (round 2, rejected): the lease counted prompt plus completion while the
    manifest read total_tokens alone, so a real call was recorded as 0 tokens. For a
    token-ceiling run that column is the only persisted record of spend in that unit."""
    assert DSN, "DATABASE_URL must be set"
    persistence = Persistence.postgres(DSN)
    made = governor({"a": None}, run_ceiling_usd=None, run_ceiling_tokens=1000)
    lease = made.lease("a")
    model = NoTotalModel()
    result = run_with(lease, model=model, persistence=persistence, pricing=None)
    assert result.status is RunStatus.COMPLETED and model.calls == 1
    assert lease.spent.tokens == CALL_TOKENS, "the lease counts the parts"
    spend, = query("SELECT budget_spend FROM execution_manifests WHERE run_id = %s", (result.run_id,))[0]
    assert spend["tokens"] == CALL_TOKENS, spend
    assert spend["node_total"]["tokens"] == CALL_TOKENS, spend


def test_a_released_node_no_longer_claims_the_reservation_it_gave_back():
    """G2: what a lease released belongs to the unallocated reserve, so the allocation
    must not still count it against the node, or it reads above the run's ceiling."""
    made = governor({"a": None, "b": None}, run_ceiling_usd=Decimal("100.00"))
    lease = made.lease("a")
    lease.charge(Usage(10, 1, 11), CALL_USD)
    lease.release()
    allocation = made.allocation
    total = allocation.orchestrator.usd + allocation.unallocated.usd + sum(amounts(allocation).values())
    assert total == Decimal("100.00"), (total, amounts(allocation))
    assert allocation.reservations["a"].usd == CALL_USD, "it keeps only what it spent"
    assert lease.reservation.usd == CALL_USD


class ReportingModel:
    """Answers with whatever usage it was given, one response per turn."""

    def __init__(self, *usages):
        self.usages = list(usages)
        self.calls = 0

    async def send(self, request):
        usage = self.usages[min(self.calls, len(self.usages) - 1)]
        self.calls += 1
        if self.calls < len(self.usages):
            # Ask for a tool nothing registered: the executor answers with an error
            # result and the loop takes another turn, so one run makes several calls.
            return ModelResponse(
                message=Message(
                    role=Role.ASSISTANT, content="",
                    tool_calls=(ToolCall(id=f"call-{self.calls}", name="absent", arguments={}),),
                ),
                stop_reason=StopReason.TOOL_CALLS,
                usage=usage,
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="done"),
            stop_reason=StopReason.END_TURN,
            usage=usage,
        )


# H1 (round 3, rejected): a provider can report anything Usage holds. None of these may
# break a run, leave it running, or make a read of the governor raise afterwards.


REPORTED_USAGE = {
    "negative parts and total": Usage(-5, 3, -2),
    "negative total only": Usage(10, 1, -2),
    "all negative": Usage(-10, -1, -11),
    "zero": Usage(0, 0, 0),
    "no total": Usage(prompt_tokens=10, completion_tokens=1),
    "total only": Usage(total_tokens=11),
    "huge": Usage(2**63, 2**63, 2**63),
    "huge negative": Usage(-(2**63), 0, -(2**63)),
    "mixed signs": Usage(-1, 2**40, 5),
}


@pytest.mark.parametrize("label", list(REPORTED_USAGE))
@pytest.mark.parametrize("ceiling", ["usd", "tokens"])
def test_any_usage_a_provider_can_report_still_ends_the_run(label, ceiling):
    """H1: the terminal path is total. Whatever a provider reports, the run reaches a
    terminal status, its row says so, and the governor stays readable afterwards."""
    assert DSN, "DATABASE_URL must be set"
    persistence = Persistence.postgres(DSN)
    made = governor(
        {"a": None},
        **({"run_ceiling_usd": Decimal("10.00")} if ceiling == "usd" else
           {"run_ceiling_usd": None, "run_ceiling_tokens": 10_000}),
    )
    lease = made.lease("a")
    model = ReportingModel(REPORTED_USAGE[label])
    result = run_with(lease, model=model, persistence=persistence)

    assert result.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.MAX_TURNS_EXCEEDED)
    status, = query("SELECT status FROM runs WHERE run_id = %s", (result.run_id,))[0]
    assert status in ("completed", "failed", "max_turns_exceeded"), status
    terminal = [
        row[0] for row in query(
            "SELECT event_type FROM run_events WHERE run_id = %s"
            " AND event_type IN ('RunCompleted','RunFailed','RunCancelled') ORDER BY sequence_no",
            (result.run_id,),
        )
    ]
    assert len(terminal) == 1, terminal
    # The governor is still readable, and its numbers are still numbers.
    assert lease.spent.tokens >= 0 and made.spend.tokens >= 0
    assert isinstance(lease.may_call(), bool)
    spend, = query("SELECT budget_spend FROM execution_manifests WHERE run_id = %s", (result.run_id,))[0]
    assert spend is not None and spend["tokens"] >= 0, spend


def test_a_negative_token_report_cannot_buy_budget():
    """H1: a provider reporting fewer than zero tokens must not refund the lease."""
    made = governor({"a": None}, run_ceiling_usd=None, run_ceiling_tokens=100)
    lease = made.lease("a")
    lease.charge(Usage(20, 5, 25), None)
    before = lease.spent.tokens
    lease.charge(Usage(-1000, 0, -1000), None)
    assert lease.spent.tokens == before, "a negative report spends nothing, and takes nothing back"
    # The same through the parts, which is the branch a provider with no total takes.
    lease.charge(Usage(prompt_tokens=-5, completion_tokens=3), None)
    assert lease.spent.tokens == before, "parts that sum below zero spend nothing either"


def test_a_lease_records_whatever_cost_it_is_charged():
    """H1: an amount records what happened. Policing belongs to the ceilings and the
    reservations, not to the record of a spend, or a correction cannot be recorded."""
    made = governor({"a": "5.00"})
    lease = made.lease("a")
    lease.charge(Usage(10, 1, 11), Decimal("0.50"))
    lease.charge(Usage(0, 0, 0), Decimal("-0.20"))  # a provider's later correction
    assert lease.spent.usd == Decimal("0.30")
    assert made.spend.usd == Decimal("0.30")
    # And a correction larger than the spend: the record holds it rather than refusing
    # to be read, which is what broke the terminal path in round 3.
    other = governor({"z": "5.00"}).lease("z")
    other.charge(Usage(0, 0, 0), Decimal("-0.50"))
    assert other.spent.usd == Decimal("-0.50")


def test_a_lease_that_breaks_during_a_run_still_leaves_a_terminal_run():
    """H1: recording a spend is a boundary, and a boundary's error path must not itself
    be able to raise (INVARIANT-73299c7c's neighbour). A lease that reads fine when the
    run is configured and breaks afterwards must still leave a terminal run and a row
    that says what happened; the budget column says nothing rather than something wrong.
    A lease that is already unreadable at the call site is refused there instead, before
    any row exists."""
    assert DSN, "DATABASE_URL must be set"
    persistence = Persistence.postgres(DSN)
    policy_in_tokens = budget().BudgetPolicy(run_ceiling_usd=None, run_ceiling_tokens=1000)

    class Governor:
        policy = policy_in_tokens

    class BreaksLater:
        node_id = "a"
        governor = Governor()
        reservation = budget().BudgetAmount(tokens=300)

        def may_call(self):
            return True

        def charge(self, usage, cost):
            return None

        @property
        def spent(self):
            raise RuntimeError("this lease cannot be read")

    before = query("SELECT count(*) FROM runs WHERE tenant_id = %s", (TENANT,))[0][0]
    result = run_with(BreaksLater(), persistence=persistence, pricing=None)
    assert result.status is RunStatus.COMPLETED, result.error
    status, = query("SELECT status FROM runs WHERE run_id = %s", (result.run_id,))[0]
    assert status == "completed"
    spend, = query("SELECT budget_spend FROM execution_manifests WHERE run_id = %s", (result.run_id,))[0]
    assert spend == {"usd": None, "tokens": None, "node_id": None, "node_total": None}, spend

    class BrokenAlready(BreaksLater):
        @property
        def governor(self):
            raise RuntimeError("this lease cannot be read")

    with pytest.raises(RuntimeError):
        run_with(BrokenAlready(), persistence=persistence, pricing=None)
    after = query("SELECT count(*) FROM runs WHERE tenant_id = %s", (TENANT,))[0][0]
    assert after == before + 1, "the refused run wrote no row"


def test_the_manifest_sums_what_each_call_counted():
    """H2: three calls, two of them reporting no total. The manifest's tokens are the
    sum of what each call counted, the same way the lease charges, and agree with
    node_total for a lease used by one run."""
    assert DSN, "DATABASE_URL must be set"
    persistence = Persistence.postgres(DSN)
    made = governor({"a": None}, run_ceiling_usd=None, run_ceiling_tokens=10_000)
    lease = made.lease("a")
    model = ReportingModel(
        Usage(prompt_tokens=10, completion_tokens=1),   # 11, no total
        Usage(20, 3, 23),                               # 23
        Usage(prompt_tokens=7, completion_tokens=2),    # 9, no total
    )
    result = run_with(lease, model=model, persistence=persistence, pricing=None, max_turns=3)
    assert model.calls == 3, model.calls
    assert lease.spent.tokens == 43
    spend, = query("SELECT budget_spend FROM execution_manifests WHERE run_id = %s", (result.run_id,))[0]
    assert spend["tokens"] == 43, spend
    assert spend["node_total"]["tokens"] == 43, spend


def test_releasing_the_orchestrator_lease_keeps_the_allocation_within_the_ceiling():
    """H3: the orchestrator's reserve is an allocation like any other."""
    made = governor({"a": None, "b": None})
    orchestrator = made.orchestrator_lease()
    orchestrator.charge(Usage(10, 1, 11), CALL_USD)
    orchestrator.release()
    allocation = made.allocation
    total = allocation.orchestrator.usd + allocation.unallocated.usd + sum(
        usd for node_id, usd in amounts(allocation).items() if node_id in ("a", "b")
    )
    assert total == Decimal("10.00"), (total, allocation)
    assert allocation.orchestrator.usd == CALL_USD


@pytest.mark.parametrize("ceiling", ["100000000000", "1E+20", "0.000000000001", "0.07"])
def test_an_extreme_usd_ceiling_still_splits(ceiling):
    """H4: tokens are bounded, USD is not. A hundred billion dollars is an unusual
    budget but an ordinary number, and it must split rather than raise a decimal error
    or be refused."""
    made = governor({"a": None, "b": None, "c": None}, run_ceiling_usd=Decimal(ceiling))
    allocation = made.allocation
    total = allocation.orchestrator.usd + allocation.unallocated.usd + sum(amounts(allocation).values())
    assert total == Decimal(ceiling), (total, ceiling)
    assert len(set(amounts(allocation).values())) == 1, "the silent nodes still share equally"


def test_a_hook_that_moves_a_call_onto_a_table_priced_model_records_the_tables_date():
    """H5: the date is recorded when the shipped table priced a call, whichever model
    the call was finally sent with (FR-69)."""
    assert DSN, "DATABASE_URL must be set"
    hooks = importlib.import_module("agentsdk.hooks")
    persistence = Persistence.postgres(DSN)

    class Reroute(hooks.RuntimeHook):
        def before_model(self, request):
            return hooks.HookOutcome(
                action=hooks.HookAction.MODIFY,
                replacement=dataclasses.replace(
                    request, model_settings={**dict(request.model_settings or {}), "model": "openai.gpt-4o-mini"}
                ),
            )

    made = governor({"a": None}, run_ceiling_usd=None, run_ceiling_tokens=1000)
    lease = made.lease("a")
    runner_with_hook = Runner(
        {"scripted": ScriptedModel()}, session_store=InMemorySessionStore(),
        model_registry=registry(None, MODEL), persistence=persistence, hook=Reroute(),
    )
    result = asyncio.run(runner_with_hook.run(
        AGENT, "spend",
        RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=1, model_override=MODEL, budget_lease=lease),
    ))
    assert result.status is RunStatus.COMPLETED
    date, = query("SELECT price_table_date FROM execution_manifests WHERE run_id = %s", (result.run_id,))[0]
    assert date == prices().PRICE_TABLE_DATE, date


def test_a_runs_recorded_spend_is_the_sum_of_its_model_calls():
    """NFR-23, on Postgres: the run row, the lease and the ModelCalled events agree."""
    assert DSN, "DATABASE_URL must be set"
    persistence = Persistence.postgres(DSN)
    made = governor({"a": "5.00"})
    lease = made.lease("a")
    result = run_with(lease, persistence=persistence, max_turns=3)
    costs = [
        Decimal(row[0]["cost_usd"])
        for row in query(
            "SELECT payload FROM run_events WHERE run_id = %s AND event_type = 'ModelCalled' ORDER BY sequence_no",
            (result.run_id,),
        )
    ]
    stored = query("SELECT cost_usd FROM runs WHERE run_id = %s", (result.run_id,))[0][0]
    assert sum(costs) == Decimal(stored) == lease.spent.usd == made.spend.usd


def test_migration_0008_adds_the_budget_columns_and_is_idempotent(monkeypatch):
    """AC-52's pattern for FR-68's migration: NULL for rows written before it."""
    assert DSN, "DATABASE_URL must be set"
    real = migrate.discover()
    assert "0008" in [version for version, _ in real], "migration 0008 is not on disk"
    name = "m17_" + uuid.uuid4().hex[:8]
    scratch = DSN + ("&" if "?" in DSN else "?") + f"options=-csearch_path%3D{name}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'SET search_path TO "{name}"')
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        monkeypatch.setattr(migrate, "discover", lambda: [m for m in real if m[0] <= "0007"])
        apply_migrations(scratch)
        run_id = str(uuid.uuid4())
        with psycopg.connect(scratch, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO runs (run_id, tenant_id, project_id, agent_spec_id, status, max_turns)"
                " VALUES (%s, %s, %s, 'm17', 'completed', 1)", (run_id, TENANT, PROJECT),
            )
            conn.execute(
                "INSERT INTO execution_manifests (run_id, tenant_id, project_id, sdk_version,"
                " agent_spec_hash, instructions_hash) VALUES (%s, %s, %s, '0', 'h', 'h')",
                (run_id, TENANT, PROJECT),
            )
        monkeypatch.setattr(migrate, "discover", lambda: real)
        assert apply_migrations(scratch) == [v for v, _ in real if v > "0007"]
        with psycopg.connect(scratch) as conn:
            columns = dict(conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = 'execution_manifests'", (name,),
            ).fetchall())
            assert columns.get("budget_policy") == "jsonb" and columns.get("budget_reservations") == "jsonb"
            assert columns.get("budget_spend") == "jsonb" and columns.get("price_table_date") == "text"
            before = conn.execute(
                "SELECT budget_policy, budget_reservations, budget_spend, price_table_date"
                " FROM execution_manifests WHERE run_id = %s", (run_id,),
            ).fetchone()
            assert before == (None, None, None, None), "a row written before 0008 claims no budget"
        assert apply_migrations(scratch) == [], "a second application changed something"
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')


# =================================================================================================
# The demo command: scripts/15_budget.py
# =================================================================================================


def test_the_example_shows_a_node_exceeding_its_reservation_offline(tmp_path):
    script = REPO / "scripts" / "15_budget.py"
    assert script.is_file(), "scripts/15_budget.py does not exist"
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "BASE_URL", "MODEL_API_KEY")}
    done = subprocess.run(
        [sys.executable, str(script), "--offline"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-1500:]
    out = done.stdout
    assert "budget_exceeded" in out, out
    assert re.search(r"reserv", out, re.I) and re.search(r"reclaim|returned", out, re.I), out
    assert "0.20" in out or "0.40" in out, "the example prints no amounts"
