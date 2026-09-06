"""match_ingredients (the search half of add_recipe_to_cart) must reduce Kroger's real
bulk_search_products response shape into one best-match-or-unmatched row per ingredient,
without ever touching the cart -- see kroger_recipe.py for why that split matters.
"""
import json

from assistant.core.kroger_recipe import KrogerRecipeClient, match_ingredients


class FakeRawClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"is_error": False, "content": [json.dumps(self._payload)]}


def _product(product_id="p1", description="Ground Beef 80/20", price=5.99, size="1 lb"):
    return {
        "product_id": product_id, "description": description, "brand": "Kroger",
        "item": {"size": size},
        "pricing": {"formatted_regular": f"${price:.2f}", "formatted_sale": None},
    }


def test_match_ingredients_picks_the_first_result_per_term():
    payload = {"results": [
        {"term": "ground beef", "success": True, "data": [_product(), _product("p2")]},
        {"term": "kidney beans", "success": True, "data": [_product("p3", "Kidney Beans", 1.29, "15 oz")]},
    ]}
    raw = FakeRawClient(payload)

    result = match_ingredients(raw, ["ground beef", "kidney beans"])

    assert raw.calls == [("bulk_search_products", {
        "searches": [{"term": "ground beef", "limit": 3}, {"term": "kidney beans", "limit": 3}],
    })]
    assert len(result["matches"]) == 2
    assert result["matches"][0] == {
        "ingredient": "ground beef", "matched": True, "product_id": "p1",
        "description": "Ground Beef 80/20", "brand": "Kroger", "size": "1 lb", "price": "$5.99",
    }
    assert result["matches"][1]["product_id"] == "p3"


def test_match_ingredients_flags_unmatched_terms():
    payload = {"results": [{"term": "unobtainium", "success": False, "message": "No products found", "data": []}]}
    raw = FakeRawClient(payload)

    result = match_ingredients(raw, ["unobtainium"])

    assert result["matches"] == [{"ingredient": "unobtainium", "matched": False}]


def test_match_ingredients_rejects_empty_list():
    result = match_ingredients(FakeRawClient({"results": []}), [])
    assert result["matches"] == []
    assert "error" in result


def test_match_ingredients_never_calls_add_to_cart():
    """The whole point of the propose/confirm split: searching must never itself write."""
    payload = {"results": [{"term": "milk", "success": True, "data": [_product("p9", "Whole Milk", 3.49, "1 gal")]}]}
    raw = FakeRawClient(payload)

    match_ingredients(raw, ["milk"])

    assert all(name != "bulk_add_to_cart" for name, _ in raw.calls)


def test_recipe_client_intercepts_only_the_synthetic_tool():
    raw = FakeRawClient({"results": [{"term": "eggs", "success": True, "data": [_product("p5", "Eggs", 2.99, "dozen")]}]})
    client = KrogerRecipeClient(raw)

    result = client.call_tool("add_recipe_to_cart", {"dish": "omelette", "ingredients": ["eggs"]})
    assert result["dish"] == "omelette"
    assert result["matches"][0]["product_id"] == "p5"

    # Every other tool name passes straight through to the real client untouched.
    passthrough = client.call_tool("view_current_cart", {})
    assert passthrough == {"is_error": False, "content": [json.dumps(raw._payload)]}
    assert raw.calls[-1] == ("view_current_cart", {})
