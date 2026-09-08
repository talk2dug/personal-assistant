"""Kitchen tools (kitchen_tools.py's save_recipe/list_recipes/etc.) dispatch through
PersonalClient exactly like personal_tools.py's own tools -- same no-gate reasoning
(writes only to Jarvis's own database, spends no money). Uses a real PersonalClient
(unlike test_engine_personal.py's FakeMCPClient) so the delegation from
PersonalClient.call_tool into kitchen_tools.dispatch is actually exercised, not just the
routing gate.
"""
import json

import pytest

from assistant.core import db, engine, kitchen_db, meal_plan_db
from assistant.core.kitchen_tools import KITCHEN_ALWAYS_TOOLS, KITCHEN_GATED_TOOLS
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


def test_save_recipe_persists_and_is_listable(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "save_recipe", "arguments": {
                "title": "Weeknight Chili",
                "ingredients": [
                    {"name": "ground beef", "quantity": "1", "unit": "lb"},
                    {"name": "kidney beans", "quantity": "1", "unit": "can"},
                ],
                "steps": ["Brown the beef.", "Add beans and simmer 20 minutes."],
            }}}
        ]},
        {"role": "assistant", "content": "Saved the chili recipe, sir."},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "save this chili recipe", personal=personal)

    assert reply == "Saved the chili recipe, sir."
    recipes = kitchen_db.list_recipes(db_path, owner_id)
    assert len(recipes) == 1
    assert recipes[0]["title"] == "Weeknight Chili"
    assert recipes[0]["ingredients"][0]["name"] == "ground beef"
    assert recipes[0]["steps"] == ["Brown the beef.", "Add beans and simmer 20 minutes."]


def test_get_update_delete_recipe_round_trip(db_path, owner_id):
    recipe_id = kitchen_db.create_recipe(
        db_path, owner_id, "Pancakes", [{"name": "flour", "quantity": "2", "unit": "cups"}], ["Mix.", "Cook."])
    personal = make_personal(db_path, owner_id)

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "get_recipe", "arguments": {"recipe_id": recipe_id}}}
        ]},
        {"role": "assistant", "content": "Here's the pancake recipe, sir."},
    ])
    reply = engine.handle_message(db_path, llm, owner_id, "show me the pancake recipe", personal=personal)
    assert reply == "Here's the pancake recipe, sir."

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "update_recipe", "arguments": {"recipe_id": recipe_id, "servings": 4}}}
        ]},
        {"role": "assistant", "content": "Updated it to 4 servings."},
    ])
    engine.handle_message(db_path, llm, owner_id, "the pancake recipe makes 4 servings", personal=personal)
    assert kitchen_db.get_recipe(db_path, owner_id, recipe_id)["servings"] == 4

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "delete_recipe", "arguments": {"recipe_id": recipe_id}}}
        ]},
        {"role": "assistant", "content": "Removed it."},
    ])
    engine.handle_message(db_path, llm, owner_id, "delete the pancake recipe", personal=personal)
    assert kitchen_db.get_recipe(db_path, owner_id, recipe_id) is None


def test_record_purchase_adds_to_inventory(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "record_purchase", "arguments": {"items": [
                {"name": "ground beef", "quantity": 2, "unit": "lb"},
                {"name": "eggs", "quantity": 12, "unit": "count"},
            ]}}}
        ]},
        {"role": "assistant", "content": "Logged the ground beef and eggs, sir."},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "I picked up 2 lbs of ground beef and a dozen eggs", personal=personal)

    assert reply == "Logged the ground beef and eggs, sir."
    beef = kitchen_db.get_inventory_item(db_path, owner_id, "ground beef")
    eggs = kitchen_db.get_inventory_item(db_path, owner_id, "eggs")
    assert beef["quantity"] == 2 and beef["unit"] == "lb"
    assert eggs["quantity"] == 12


def test_update_inventory_quantity_sets_an_absolute_amount(db_path, owner_id):
    kitchen_db.upsert_inventory_item(db_path, owner_id, "rice", quantity_delta=10, unit="cups")
    personal = make_personal(db_path, owner_id)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "update_inventory_quantity", "arguments": {"item": "rice", "quantity": 2}}}
        ]},
        {"role": "assistant", "content": "Updated rice to 2 cups."},
    ])

    engine.handle_message(db_path, llm, owner_id, "we're down to about 2 cups of rice", personal=personal)

    assert kitchen_db.get_inventory_item(db_path, owner_id, "rice")["quantity"] == 2


def test_list_and_remove_inventory_item(db_path, owner_id):
    kitchen_db.upsert_inventory_item(db_path, owner_id, "kale", quantity_delta=1, unit="bunch")
    personal = make_personal(db_path, owner_id)

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "list_kitchen_inventory", "arguments": {}}}
        ]},
        {"role": "assistant", "content": "You have kale on hand."},
    ])
    assert engine.handle_message(db_path, llm, owner_id, "what's in the kitchen", personal=personal) == "You have kale on hand."

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "remove_inventory_item", "arguments": {"item": "kale"}}}
        ]},
        {"role": "assistant", "content": "Stopped tracking kale."},
    ])
    engine.handle_message(db_path, llm, owner_id, "stop tracking kale", personal=personal)
    assert kitchen_db.get_inventory_item(db_path, owner_id, "kale") is None


class FakeKrogerMCPClient:
    """Mirrors test_kitchen_db.py's fake -- this file only needs to prove
    sync_kroger_purchases dispatches through to kitchen_db.sync_kroger_orders with the
    right kroger client, not re-exercise that function's own logic."""

    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "view_order_history":
            payload = {"success": True, "orders": [], "showing": 0, "summary": {"total_orders": 0}}
            return {"is_error": False, "content": [json.dumps(payload)]}
        raise ValueError(f"unexpected tool: {name}")


def test_sync_kroger_purchases_dispatches_through_to_kroger_client(db_path, owner_id):
    kroger_client = FakeKrogerMCPClient()
    personal = engine.PersonalContext(mcp_client=PersonalClient(db_path, owner_id, kroger=kroger_client))
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "sync_kroger_purchases", "arguments": {}}}
        ]},
        {"role": "assistant", "content": "Checked -- nothing new from Kroger."},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "did my kroger order sync yet", personal=personal)

    assert reply == "Checked -- nothing new from Kroger."
    assert kroger_client.calls == [("view_order_history", {"limit": 50})]


def test_sync_kroger_purchases_without_kroger_configured_reports_that_clearly(db_path, owner_id):
    personal = make_personal(db_path, owner_id)  # no kroger= passed
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "sync_kroger_purchases", "arguments": {}}}
        ]},
        {"role": "assistant", "content": "Kroger isn't set up, sir."},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "sync my kroger order", personal=personal)
    assert reply == "Kroger isn't set up, sir."


def test_cook_recipe_then_apply_deduction_via_chat(db_path, owner_id):
    recipe_id = kitchen_db.create_recipe(
        db_path, owner_id, "Pancakes", [{"name": "flour", "quantity": "2", "unit": "cups"}], ["Mix.", "Cook."])
    kitchen_db.upsert_inventory_item(db_path, owner_id, "flour", quantity_set=5, unit="cups")
    personal = make_personal(db_path, owner_id)

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "cook_recipe", "arguments": {"recipe_id": recipe_id}}}
        ]},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "apply_recipe_deduction", "arguments": {
                "recipe_id": recipe_id, "deductions": [{"item": "flour", "quantity_used": 2, "unit": "cups"}],
            }}}
        ]},
        {"role": "assistant", "content": "Used 2 cups of flour for the pancakes."},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "I made the pancakes tonight", personal=personal)

    assert reply == "Used 2 cups of flour for the pancakes."
    assert kitchen_db.get_inventory_item(db_path, owner_id, "flour")["quantity"] == 3


def test_list_makeable_recipes_via_chat(db_path, owner_id):
    kitchen_db.create_recipe(
        db_path, owner_id, "Cereal", [{"name": "milk", "quantity": "1", "unit": "cup"}], ["Pour."])
    kitchen_db.upsert_inventory_item(db_path, owner_id, "milk", quantity_set=1, unit="cup")
    personal = make_personal(db_path, owner_id)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "list_makeable_recipes", "arguments": {}}}
        ]},
        {"role": "assistant", "content": "You could make cereal right now."},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "what can I make tonight", personal=personal)
    assert reply == "You could make cereal right now."


def test_shopping_list_tools_via_chat(db_path, owner_id):
    personal = make_personal(db_path, owner_id)

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "add_to_shopping_list", "arguments": {"item": "paper towels"}}}
        ]},
        {"role": "assistant", "content": "Added paper towels to the shopping list."},
    ])
    engine.handle_message(db_path, llm, owner_id, "add paper towels to the shopping list", personal=personal)
    assert [r["item"] for r in kitchen_db.list_shopping_list(db_path, owner_id)] == ["paper towels"]

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "list_shopping_list", "arguments": {}}}
        ]},
        {"role": "assistant", "content": "Just paper towels on the list."},
    ])
    assert engine.handle_message(db_path, llm, owner_id, "what's on the shopping list", personal=personal) == "Just paper towels on the list."

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "mark_shopping_list_item_purchased", "arguments": {"item": "paper towels"}}}
        ]},
        {"role": "assistant", "content": "Crossed off paper towels."},
    ])
    engine.handle_message(db_path, llm, owner_id, "I got the paper towels", personal=personal)
    assert kitchen_db.list_shopping_list(db_path, owner_id) == []


def test_display_recipe_via_chat_queues_a_pending_view_for_the_kitchen_screen(db_path, owner_id):
    recipe_id = kitchen_db.create_recipe(
        db_path, owner_id, "Pancakes", [{"name": "flour", "quantity": "2", "unit": "cups"}], ["Mix.", "Cook."])
    personal = make_personal(db_path, owner_id)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "display_recipe", "arguments": {"recipe_id": recipe_id}}}
        ]},
        {"role": "assistant", "content": "Pulled up the pancake recipe on the kitchen screen."},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "show the pancake recipe on the kitchen screen", personal=personal)

    assert reply == "Pulled up the pancake recipe on the kitchen screen."
    view = kitchen_db.pop_pending_recipe_view(db_path, "laptop1")
    assert view["title"] == "Pancakes"
    assert view["ingredients"] == [{"name": "flour", "quantity": "2", "unit": "cups"}]
    assert view["steps"] == ["Mix.", "Cook."]


def test_display_recipe_unknown_recipe_reports_an_error(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "display_recipe", "arguments": {"recipe_id": 999}}}
        ]},
        {"role": "assistant", "content": "I couldn't find that recipe, sir."},
    ])

    engine.handle_message(db_path, llm, owner_id, "show recipe 999 on the kitchen screen", personal=personal)
    assert kitchen_db.pop_pending_recipe_view(db_path, "laptop1") is None


def test_display_recipe_unknown_location_reports_an_error(db_path, owner_id):
    recipe_id = kitchen_db.create_recipe(db_path, owner_id, "Pancakes", [{"name": "flour"}], ["Mix."])
    personal = make_personal(db_path, owner_id)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "display_recipe", "arguments": {"recipe_id": recipe_id, "location": "garage"}}}
        ]},
        {"role": "assistant", "content": "There's no screen in the garage, sir."},
    ])

    engine.handle_message(db_path, llm, owner_id, "show the pancakes recipe in the garage", personal=personal)
    assert kitchen_db.pop_pending_recipe_view(db_path, "laptop1") is None


def test_meal_plan_full_flow_via_chat(db_path, owner_id):
    db.create_manual_recurring_charge(
        db_path, owner_id, "Paycheck (15th)", 4000.0, "income", "monthly_on_day", "2026-09-15")
    db.create_manual_recurring_charge(
        db_path, owner_id, "Paycheck (last day)", 4000.0, "income", "monthly_on_last_day", "2026-09-30")
    personal = make_personal(db_path, owner_id)

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "get_pay_period", "arguments": {}}}
        ]},
        {"role": "assistant", "content": "You're in the Sep 15 - Sep 30 pay period. Want to plan meals for it?"},
    ])
    reply = engine.handle_message(db_path, llm, owner_id, "let's plan meals for this pay period", personal=personal)
    assert "Sep" in reply

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "start_meal_plan", "arguments": {
                "period_start": "2026-09-15", "period_end": "2026-09-30",
            }}}
        ]},
        {"role": "assistant", "content": "Started the plan."},
    ])
    engine.handle_message(db_path, llm, owner_id, "start the plan", personal=personal)
    plan = meal_plan_db.get_current_meal_plan(db_path, owner_id)
    assert plan["status"] == "draft"
    assert plan["max_deliveries"] == 2

    plan_id = plan["id"]
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "add_meal_plan_entry", "arguments": {
                "meal_plan_id": plan_id, "plan_date": "2026-09-16", "meal_type": "dinner",
                "title": "Weeknight Chili", "servings_planned": 4,
            }}}
        ]},
        {"role": "assistant", "content": "Chili's on for the 16th."},
    ])
    engine.handle_message(db_path, llm, owner_id, "let's do chili on the 16th", personal=personal)
    entries = meal_plan_db.list_meal_plan_entries(db_path, owner_id, plan_id)
    assert entries[0]["title"] == "Weeknight Chili"

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "list_meal_plan", "arguments": {}}}
        ]},
        {"role": "assistant", "content": "So far you've got chili on the 16th."},
    ])
    assert engine.handle_message(
        db_path, llm, owner_id, "what's the plan look like so far", personal=personal,
    ) == "So far you've got chili on the 16th."

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "finalize_meal_plan", "arguments": {"meal_plan_id": plan_id}}}
        ]},
        {"role": "assistant", "content": "Locked it in."},
    ])
    engine.handle_message(db_path, llm, owner_id, "that's the plan, lock it in", personal=personal)
    assert meal_plan_db.get_meal_plan(db_path, owner_id, plan_id)["status"] == "active"


def test_remove_meal_plan_entry_via_chat(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    entry = meal_plan_db.add_meal_plan_entry(db_path, owner_id, plan_id, "2026-09-16", "dinner", "Chili")
    personal = make_personal(db_path, owner_id)

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "remove_meal_plan_entry", "arguments": {"entry_id": entry["id"]}}}
        ]},
        {"role": "assistant", "content": "Took chili off the 16th."},
    ])
    engine.handle_message(db_path, llm, owner_id, "actually skip the chili on the 16th", personal=personal)
    assert meal_plan_db.list_meal_plan_entries(db_path, owner_id, plan_id) == []


def test_meal_plan_shopping_list_flow_via_chat(db_path, owner_id):
    recipe_id = kitchen_db.create_recipe(
        db_path, owner_id, "Weeknight Chili", [{"name": "ground beef", "quantity": "1", "unit": "lb"}], ["Brown it."])
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.add_meal_plan_entry(
        db_path, owner_id, plan_id, "2026-09-16", "dinner", "Weeknight Chili", recipe_id=recipe_id)
    personal = make_personal(db_path, owner_id)

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "generate_meal_plan_shopping_list", "arguments": {"meal_plan_id": plan_id}}}
        ]},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "save_meal_plan_shopping_items", "arguments": {
                "meal_plan_id": plan_id,
                "items": [{"item": "ground beef", "quantity_to_buy": "1 lb", "category": "meat"}],
            }}}
        ]},
        {"role": "assistant", "content": "Shopping list is just ground beef, 1 lb."},
    ])
    reply = engine.handle_message(db_path, llm, owner_id, "what do I need to buy for this plan", personal=personal)
    assert reply == "Shopping list is just ground beef, 1 lb."
    saved = meal_plan_db.list_meal_plan_shopping_items(db_path, owner_id, plan_id)
    assert saved[0]["item"] == "ground beef"


class FakeKrogerClientForDispatch:
    def call_tool(self, name, arguments):
        import json
        payload = {"results": [{"term": "ground beef", "success": True, "data": [{
            "product_id": "p1", "description": "Ground Beef 80/20", "brand": "Kroger",
            "item": {"size": "1 lb"},
            "pricing": {"formatted_regular": "$5.99", "formatted_sale": None, "on_sale": False},
        }]}]}
        return {"is_error": False, "content": [json.dumps(payload)]}


def test_match_meal_plan_items_to_kroger_via_chat(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.save_meal_plan_shopping_items(db_path, owner_id, plan_id, [{"item": "ground beef"}])
    personal = engine.PersonalContext(mcp_client=PersonalClient(db_path, owner_id, kroger=FakeKrogerClientForDispatch()))

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "match_meal_plan_items_to_kroger", "arguments": {"meal_plan_id": plan_id}}}
        ]},
        {"role": "assistant", "content": "Found ground beef 80/20 for $5.99."},
    ])
    reply = engine.handle_message(db_path, llm, owner_id, "match my shopping list to kroger", personal=personal)
    assert reply == "Found ground beef 80/20 for $5.99."


def test_match_meal_plan_items_to_kroger_without_kroger_configured(db_path, owner_id):
    plan_id = meal_plan_db.create_meal_plan(db_path, owner_id, "2026-09-15", "2026-09-30")
    meal_plan_db.save_meal_plan_shopping_items(db_path, owner_id, plan_id, [{"item": "ground beef"}])
    personal = make_personal(db_path, owner_id)  # no kroger=

    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "match_meal_plan_items_to_kroger", "arguments": {"meal_plan_id": plan_id}}}
        ]},
        {"role": "assistant", "content": "Kroger isn't set up, sir."},
    ])
    reply = engine.handle_message(db_path, llm, owner_id, "match my shopping list to kroger", personal=personal)
    assert reply == "Kroger isn't set up, sir."


def test_payday_keyword_alone_gates_in_meal_plan_tools(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    names = {t["function"]["name"] for t in engine.select_tools("when's my next payday", personal=personal)}
    assert "get_pay_period" in names
    assert "start_meal_plan" in names


def test_kitchen_tools_absent_when_no_personal_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])
    reply = engine.handle_message(db_path, llm, owner_id, "save this recipe: ...", personal=None)
    assert reply == "Hi there"


def test_always_tools_present_without_a_kitchen_keyword(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    names = {t["function"]["name"] for t in engine.select_tools("what time is it", personal=personal)}
    assert "save_recipe" in names
    assert "list_recipes" not in names


def test_gated_tools_present_with_a_kitchen_keyword(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    names = {t["function"]["name"] for t in engine.select_tools("what recipes do I have", personal=personal)}
    assert {t["function"]["name"] for t in KITCHEN_GATED_TOOLS} <= names


def test_kroger_keyword_alone_gates_in_sync_kroger_purchases(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    names = {t["function"]["name"] for t in engine.select_tools("did my kroger order go through", personal=personal)}
    assert "sync_kroger_purchases" in names


def test_pull_up_keyword_alone_gates_in_display_recipe(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    names = {t["function"]["name"] for t in engine.select_tools("pull up the chili recipe", personal=personal)}
    assert "display_recipe" in names


def test_unrouted_tools_include_full_kitchen_catalog(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    names = {t["function"]["name"] for t in engine.select_tools("anything", personal=personal, route=False)}
    all_kitchen = {t["function"]["name"] for t in KITCHEN_ALWAYS_TOOLS + KITCHEN_GATED_TOOLS}
    assert all_kitchen <= names
