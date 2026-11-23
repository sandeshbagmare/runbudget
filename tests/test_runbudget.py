"""Tests for runbudget."""

import pytest

from runbudget import Budget, BudgetExceeded, ModelPrice, RunBudget, Status


class TestModelPrice:
    def test_cost_of_a_million_tokens(self):
        assert ModelPrice(3.0, 15.0).cost(1_000_000, 0) == 3.0
        assert ModelPrice(3.0, 15.0).cost(0, 1_000_000) == 15.0

    def test_cost_of_a_small_call(self):
        assert ModelPrice(0.15, 0.60).cost(1_000, 500) == 0.00045

    def test_zero_tokens(self):
        assert ModelPrice(1.0, 2.0).cost(0, 0) == 0.0


class TestBasics:
    def test_an_empty_budget_is_ok(self):
        assert RunBudget().status().action == "ok"

    def test_spend_and_tokens_accumulate(self):
        budget = RunBudget()
        budget.add("gpt-4o-mini", 1000, 500)
        budget.add("gpt-4o-mini", 1000, 500)
        assert budget.tokens == 3000
        assert budget.steps == 2
        assert budget.spent == round(2 * (1000 * 0.15 + 500 * 0.60) / 1_000_000, 6)

    def test_a_known_model_costs_what_the_table_says(self):
        budget = RunBudget()
        status = budget.add("gpt-4o", 1_000_000, 0)
        assert status.spent == 2.5

    def test_a_prefix_matches_a_family(self):
        budget = RunBudget()
        assert budget.add("gpt-4o-2026-01", 1_000_000, 0).spent == 2.5

    def test_an_unknown_model_is_an_error(self):
        with pytest.raises(KeyError, match="no price for model"):
            RunBudget().add("mystery-model", 10, 10)

    def test_prices_can_be_supplied(self):
        budget = RunBudget(prices={"mystery": (1.0, 2.0)})
        assert budget.add("mystery", 1_000_000, 1_000_000).spent == 3.0

    def test_a_price_can_be_a_model_price_object(self):
        budget = RunBudget(prices={"mystery": ModelPrice(1.0, 2.0)})
        assert budget.add("mystery", 1_000_000, 0).spent == 1.0

    def test_an_explicit_cost_is_used(self):
        assert RunBudget().add("gpt-4o", 1_000_000, 0, cost=0.01).spent == 0.01

    def test_a_paid_tool_can_be_charged(self):
        budget = RunBudget()
        assert budget.add_cost(0.02, model="web_search").spent == 0.02
        assert budget.breakdown()["web_search"]["cost"] == 0.02

    def test_negative_tokens_are_rejected(self):
        with pytest.raises(ValueError, match="negative"):
            RunBudget().add("gpt-4o", -1, 0)

    def test_a_negative_extra_cost_is_rejected(self):
        with pytest.raises(ValueError, match="negative"):
            RunBudget().add_cost(-1)

    def test_reset(self):
        budget = RunBudget()
        budget.add("gpt-4o", 1000, 1000)
        budget.reset()
        assert (budget.spent, budget.tokens, budget.steps) == (0.0, 0, 0)


class TestLimits:
    def test_a_cost_limit_stops_the_run(self):
        budget = RunBudget(budget=Budget(max_cost=0.01))
        assert budget.add("gpt-4o", 1_000, 100).action == "ok"
        assert budget.add("gpt-4o", 10_000, 1_000).action == "stop"

    def test_the_stop_reason_names_the_limit(self):
        budget = RunBudget(budget=Budget(max_steps=1))
        status = budget.add("gpt-4o", 1, 1)
        assert status.reason == "steps budget reached: 1 of 1"

    def test_a_token_limit(self):
        budget = RunBudget(budget=Budget(max_tokens=100))
        assert budget.add("gpt-4o", 60, 10).action == "ok"
        status = budget.add("gpt-4o", 30, 10)
        assert status.action == "stop"
        assert "110 of 100" in status.reason

    def test_a_step_limit(self):
        budget = RunBudget(budget=Budget(max_steps=2))
        assert budget.add("gpt-4o", 1, 1).action == "ok"
        assert budget.add("gpt-4o", 1, 1).action == "stop"

    def test_a_warning_before_the_limit(self):
        budget = RunBudget(budget=Budget(max_cost=0.01), warn_at=0.8)
        status = budget.add("gpt-4o", 3_500, 0)
        assert status.action == "warn"
        assert "budget at 88%" in status.reason

    def test_the_warning_threshold_is_configurable(self):
        budget = RunBudget(budget=Budget(max_cost=1.0), warn_at=0.5)
        assert budget.add("gpt-4o", 300_000, 0).action == "warn"

    def test_a_bad_threshold_is_rejected(self):
        with pytest.raises(ValueError, match="warn_at"):
            RunBudget(warn_at=0)

    def test_no_limits_never_stops(self):
        budget = RunBudget()
        for _ in range(50):
            assert budget.add("gpt-4o", 1000, 1000).action == "ok"

    def test_a_status_is_false_only_when_stopped(self):
        budget = RunBudget(budget=Budget(max_steps=1))
        assert bool(budget.add("gpt-4o", 1, 1)) is False
        assert Status("ok").allowed is True
        assert Status("warn").stopped is False

    def test_str_of_a_status(self):
        assert str(Status("ok")) == "ok"
        assert str(Status("stop", "spent")) == "stop: spent"


class TestEnforce:
    def test_check_passes_within_budget(self):
        RunBudget(budget=Budget(max_steps=2)).check()

    def test_check_raises_when_spent(self):
        budget = RunBudget(budget=Budget(max_steps=1))
        budget.add("gpt-4o", 1, 1)
        with pytest.raises(BudgetExceeded) as caught:
            budget.check()
        assert caught.value.status.action == "stop"
        assert caught.value.reason == "steps budget reached: 1 of 1"

    def test_enforce_is_the_same_call(self):
        budget = RunBudget(budget=Budget(max_steps=1))
        budget.add("gpt-4o", 1, 1)
        with pytest.raises(BudgetExceeded):
            budget.enforce()

    def test_it_is_a_runtime_error(self):
        assert issubclass(BudgetExceeded, RuntimeError)


class TestReading:
    def test_remaining_cost(self):
        budget = RunBudget(budget=Budget(max_cost=1.0))
        budget.add("gpt-4o", 100_000, 0)
        assert budget.remaining.max_cost == 0.75

    def test_remaining_never_goes_below_zero(self):
        budget = RunBudget(budget=Budget(max_steps=1))
        budget.add("gpt-4o", 1, 1)
        assert budget.remaining.max_steps == 0

    def test_unlimited_remaining_stays_none(self):
        budget = RunBudget(budget=Budget(max_steps=3))
        assert budget.remaining.max_cost is None
        assert budget.remaining.max_tokens is None

    def test_breakdown_per_model(self):
        budget = RunBudget()
        budget.add("gpt-4o", 1_000_000, 0)
        budget.add("gpt-4o", 1_000_000, 0)
        budget.add("gpt-4o-mini", 1_000_000, 0)
        breakdown = budget.breakdown()
        assert breakdown["gpt-4o"]["steps"] == 2
        assert breakdown["gpt-4o"]["cost"] == 5.0
        assert breakdown["gpt-4o-mini"]["cost"] == 0.15
        assert breakdown["gpt-4o"]["input_tokens"] == 2_000_000

    def test_breakdown_of_an_empty_run(self):
        assert RunBudget().breakdown() == {}


class TestMiddlewareStyleUse:
    def test_a_loop_that_stops_on_budget(self):
        budget = RunBudget(budget=Budget(max_steps=3))
        calls = []
        while budget.status().allowed:
            budget.add("gpt-4o-mini", 100, 50)
            calls.append(1)
        assert len(calls) == 3
        assert budget.status().reason == "steps budget reached: 3 of 3"

    def test_the_stop_message_can_be_shown_to_the_user(self):
        budget = RunBudget(budget=Budget(max_cost=0.001))
        budget.add("gpt-4o", 1_000, 0)
        status = budget.status()
        assert status.stopped
        assert "cost budget reached" in status.reason
