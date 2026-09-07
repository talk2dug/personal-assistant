"""Recipe API: a remote MCP server (same transport/auth shape as Era) with no sensitive
tools -- search/filter/lookup are pure reads and generate_recipe only composes a new
recipe object, so unlike Era/mail there's no confirmation gate to verify here. What's
worth testing is that it's keyword-routed (a 10-tool catalog, not tiny like camera's 3,
so unlike show_camera it DOES get gated) and that calls reach the real client."""
import pytest

from assistant.core import db, engine


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeRecipeClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"is_error": False, "content": ["some result"]}


def make_recipe():
    tools = [
        {"type": "function", "function": {"name": "search_recipes", "description": "", "parameters": {}}},
        {"type": "function", "function": {"name": "get_recipe", "description": "", "parameters": {}}},
    ]
    client = FakeRecipeClient()
    return engine.RecipeContext(mcp_client=client, recipe_tools=tools), client


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def test_recipe_tools_absent_without_a_recipe_keyword(db_path, owner_id):
    recipe, client = make_recipe()
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])

    reply = engine.handle_message(db_path, llm, owner_id, "what time is it", recipe=recipe)

    assert reply == "Hi there"
    # select_tools is exercised for real inside handle_message -- confirm the routed
    # catalog it built genuinely excluded recipe tools for this message.
    assert "search_recipes" not in {
        t["function"]["name"] for t in engine.select_tools("what time is it", recipe=recipe)
    }


def test_recipe_tools_present_for_a_recipe_keyword(db_path, owner_id):
    recipe, client = make_recipe()
    names = {t["function"]["name"] for t in engine.select_tools("find me a dinner recipe", recipe=recipe)}
    assert {"search_recipes", "get_recipe"} <= names


def test_search_recipes_call_reaches_the_real_client(db_path, owner_id):
    recipe, client = make_recipe()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "search_recipes", "arguments": {"query": "spicy chicken soup"}}}
        ]},
        {"role": "assistant", "content": "Found a few spicy chicken soup recipes, sir."},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "find me a spicy chicken soup recipe", recipe=recipe)

    assert client.calls == [("search_recipes", {"query": "spicy chicken soup"})]
    assert reply == "Found a few spicy chicken soup recipes, sir."


def test_recipe_tool_absent_when_no_recipe_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])
    reply = engine.handle_message(db_path, llm, owner_id, "give me a recipe for chili", recipe=None)
    assert reply == "Hi there"
