"""Kitchen tools (kitchen_tools.py's save_recipe/list_recipes/etc.) dispatch through
PersonalClient exactly like personal_tools.py's own tools -- same no-gate reasoning
(writes only to Jarvis's own database, spends no money). Uses a real PersonalClient
(unlike test_engine_personal.py's FakeMCPClient) so the delegation from
PersonalClient.call_tool into kitchen_tools.dispatch is actually exercised, not just the
routing gate.
"""
import pytest

from assistant.core import db, engine, kitchen_db
from assistant.core.kitchen_tools import KITCHEN_ALWAYS_TOOLS, KITCHEN_GATED_TOOLS
from assistant.core.personal_tools import PersonalClient


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    kitchen_db.init_kitchen_db(path)
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


def test_unrouted_tools_include_full_kitchen_catalog(db_path, owner_id):
    personal = make_personal(db_path, owner_id)
    names = {t["function"]["name"] for t in engine.select_tools("anything", personal=personal, route=False)}
    all_kitchen = {t["function"]["name"] for t in KITCHEN_ALWAYS_TOOLS + KITCHEN_GATED_TOOLS}
    assert all_kitchen <= names
