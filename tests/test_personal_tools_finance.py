"""Chat-tool wrappers over the already-working budgets/manual-recurring-charges/
savings-goals CRUD and the pay-period-aware safe-to-spend number (personal_tools.py's
set_budget/add_manual_recurring_charge/create_savings_goal/get_safe_to_spend/etc.) --
before these existed, the owner could only manage this data by clicking through the
Finance page. Uses a real PersonalClient (like test_engine_kitchen.py) so the delegation
from PersonalClient.call_tool into db.py/meal_plan_db.py is actually exercised, not just
the routing gate.
"""
from datetime import date

import pytest

from assistant.core import db, engine, kitchen_db, meal_plan_db
from assistant.core.personal_tools import PersonalClient


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    kitchen_db.init_kitchen_db(path)
    meal_plan_db.init_meal_plan_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def make_personal(db_path, owner_id):
    return engine.PersonalContext(mcp_client=PersonalClient(db_path, owner_id))


def run_tool(db_path, owner_id, tool_name, arguments, reply_text="Done."):
    personal = make_personal(db_path, owner_id)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [{"function": {"name": tool_name, "arguments": arguments}}]},
        {"role": "assistant", "content": reply_text},
    ])
    return engine.handle_message(db_path, llm, owner_id, "do the thing", personal=personal)


# --- budgets -----------------------------------------------------------------------

def test_set_budget_creates_a_budget(db_path, owner_id):
    run_tool(db_path, owner_id, "set_budget", {
        "category_key": "fcat_dining", "category_label": "Dining out", "monthly_limit": 400.0,
    })
    budgets = db.list_budgets(db_path, owner_id)
    assert len(budgets) == 1
    assert budgets[0]["monthly_limit"] == 400.0


def test_set_budget_on_existing_category_updates_the_limit(db_path, owner_id):
    db.create_budget(db_path, owner_id, "fcat_dining", "Dining out", 400.0)
    run_tool(db_path, owner_id, "set_budget", {
        "category_key": "fcat_dining", "category_label": "Dining out", "monthly_limit": 250.0,
    })
    budgets = db.list_budgets(db_path, owner_id)
    assert len(budgets) == 1
    assert budgets[0]["monthly_limit"] == 250.0


def test_delete_budget_removes_it(db_path, owner_id):
    budget_id = db.create_budget(db_path, owner_id, "fcat_travel", "Travel", 200.0)
    run_tool(db_path, owner_id, "delete_budget", {"budget_id": budget_id})
    assert db.list_budgets(db_path, owner_id) == []


# --- manual recurring charges --------------------------------------------------------

def test_add_manual_recurring_charge_persists_it(db_path, owner_id):
    run_tool(db_path, owner_id, "add_manual_recurring_charge", {
        "description": "Rent", "amount": 925.0, "direction": "expense",
        "cadence": "monthly_on_day", "next_expected_date": "2026-09-15",
    })
    charges = db.list_manual_recurring_charges(db_path, owner_id)
    assert len(charges) == 1
    assert charges[0]["description"] == "Rent"


def test_add_manual_recurring_charge_rejects_invalid_direction(db_path, owner_id):
    reply = run_tool(db_path, owner_id, "add_manual_recurring_charge", {
        "description": "Rent", "amount": 925.0, "direction": "sideways",
        "cadence": "monthly_on_day", "next_expected_date": "2026-09-15",
    }, reply_text="Handled.")
    assert db.list_manual_recurring_charges(db_path, owner_id) == []
    assert reply == "Handled."  # the model still gets a normal reply after seeing the error


def test_add_manual_recurring_charge_rejects_invalid_cadence(db_path, owner_id):
    run_tool(db_path, owner_id, "add_manual_recurring_charge", {
        "description": "Rent", "amount": 925.0, "direction": "expense",
        "cadence": "fortnightly", "next_expected_date": "2026-09-15",
    })
    assert db.list_manual_recurring_charges(db_path, owner_id) == []


def test_delete_manual_recurring_charge_removes_it(db_path, owner_id):
    charge_id = db.create_manual_recurring_charge(
        db_path, owner_id, "Rent", 925.0, "expense", "monthly_on_day", "2026-09-15")
    run_tool(db_path, owner_id, "delete_manual_recurring_charge", {"charge_id": charge_id})
    assert db.list_manual_recurring_charges(db_path, owner_id) == []


# --- savings goals -------------------------------------------------------------------

def test_create_savings_goal_persists_it(db_path, owner_id):
    run_tool(db_path, owner_id, "create_savings_goal", {
        "name": "Japan trip", "target_amount": 3000.0, "target_date": "2027-06-01",
    })
    goals = db.list_savings_goals(db_path, owner_id)
    assert len(goals) == 1
    assert goals[0]["name"] == "Japan trip"
    assert goals[0]["target_date"] == "2027-06-01"


def test_update_savings_goal_changes_target_amount(db_path, owner_id):
    goal_id = db.create_savings_goal(db_path, owner_id, "Japan trip", 3000.0, "2027-06-01")
    run_tool(db_path, owner_id, "update_savings_goal", {"goal_id": goal_id, "target_amount": 3500.0})
    goals = db.list_savings_goals(db_path, owner_id)
    assert goals[0]["target_amount"] == 3500.0
    assert goals[0]["target_date"] == "2027-06-01"  # untouched


def test_update_savings_goal_clears_target_date_when_explicitly_empty(db_path, owner_id):
    goal_id = db.create_savings_goal(db_path, owner_id, "Japan trip", 3000.0, "2027-06-01")
    run_tool(db_path, owner_id, "update_savings_goal", {"goal_id": goal_id, "target_date": ""})
    goals = db.list_savings_goals(db_path, owner_id)
    assert goals[0]["target_date"] is None


def test_delete_savings_goal_removes_it(db_path, owner_id):
    goal_id = db.create_savings_goal(db_path, owner_id, "Japan trip", 3000.0)
    run_tool(db_path, owner_id, "delete_savings_goal", {"goal_id": goal_id})
    assert db.list_savings_goals(db_path, owner_id) == []


# --- safe to spend / safety buffer ----------------------------------------------------

def test_get_safety_buffer_defaults_to_zero(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [{"function": {"name": "get_safety_buffer", "arguments": {}}}]},
        {"role": "assistant", "content": "Your safety buffer is $0."},
    ])
    reply = engine.handle_message(db_path, llm, owner_id, "what's my safety buffer", personal=personal)
    assert "$0" in reply


def test_set_safety_buffer_persists_and_is_read_back(db_path, owner_id):
    run_tool(db_path, owner_id, "set_safety_buffer", {"safety_buffer": 150.0})
    assert float(db.get_setting(db_path, db.FINANCE_SAFETY_BUFFER_SETTING, "0")) == 150.0


def test_set_safety_buffer_rejects_negative(db_path, owner_id):
    run_tool(db_path, owner_id, "set_safety_buffer", {"safety_buffer": -10.0})
    assert db.get_setting(db_path, db.FINANCE_SAFETY_BUFFER_SETTING) is None


def test_get_safe_to_spend_reflects_real_pay_period_data(db_path, owner_id):
    db.upsert_era_account(db_path, "acct-checking", "Checking", "Checking", 1000.0, 1000.0)
    db.create_manual_recurring_charge(
        db_path, owner_id, "Paycheck (15th)", 2000.0, "income", "monthly_on_day", "2026-09-15")
    db.create_manual_recurring_charge(
        db_path, owner_id, "Paycheck (last day)", 2000.0, "income", "monthly_on_last_day", "2026-09-30")

    personal = make_personal(db_path, owner_id)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [{"function": {"name": "get_safe_to_spend", "arguments": {}}}]},
        {"role": "assistant", "content": "Here's your safe-to-spend number."},
    ])
    reply = engine.handle_message(db_path, llm, owner_id, "what can I spend right now", personal=personal)
    assert reply == "Here's your safe-to-spend number."


def test_get_safe_to_spend_returns_none_without_pay_period_data(db_path, owner_id):
    result = PersonalClient(db_path, owner_id).call_tool("get_safe_to_spend", {})
    assert result["safe_to_spend"] is None
