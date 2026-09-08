"""match_ingredients (the search half of add_recipe_to_cart) must reduce Kroger's real
bulk_search_products response shape into one best-match-or-unmatched row per ingredient,
without ever touching the cart -- see kroger_recipe.py for why that split matters.
check_deals (a sibling synthetic tool, for meal planning) reduces the same real response
shape down to only the results that are actually on sale.
"""
import json

from assistant.core.kroger_recipe import KrogerRecipeClient, check_deals, match_ingredients


class FakeRawClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"is_error": False, "content": [json.dumps(self._payload)]}


def _product(product_id="p1", description="Ground Beef 80/20", price=5.99, size="1 lb", on_sale=False, sale_price=None):
    return {
        "product_id": product_id, "description": description, "brand": "Kroger",
        "item": {"size": size},
        "pricing": {
            "formatted_regular": f"${price:.2f}",
            "formatted_sale": f"${sale_price:.2f}" if sale_price is not None else None,
            "on_sale": on_sale,
        },
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
        "on_sale": False,
    }
    assert result["matches"][1]["product_id"] == "p3"


def test_match_ingredients_surfaces_on_sale():
    payload = {"results": [
        {"term": "chicken breast", "success": True, "data": [
            _product("p1", "Chicken Breast", 6.99, "1 lb", on_sale=True, sale_price=4.99),
        ]},
    ]}
    result = match_ingredients(FakeRawClient(payload), ["chicken breast"])
    assert result["matches"][0]["on_sale"] is True
    assert result["matches"][0]["price"] == "$4.99"


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


# --- check_deals ---------------------------------------------------------------

def test_check_deals_returns_only_on_sale_results():
    payload = {"results": [
        {"term": "chicken breast", "success": True, "data": [
            _product("p1", "Chicken Breast", 6.99, "1 lb", on_sale=True, sale_price=4.99),
        ]},
        {"term": "broccoli", "success": True, "data": [
            _product("p2", "Broccoli Crowns", 2.49, "1 lb", on_sale=False),
        ]},
    ]}
    raw = FakeRawClient(payload)

    result = check_deals(raw, ["chicken breast", "broccoli"])

    assert raw.calls == [("bulk_search_products", {
        "searches": [{"term": "chicken breast", "limit": 5}, {"term": "broccoli", "limit": 5}],
    })]
    assert result["deals"] == [{
        "term": "chicken breast", "product_id": "p1", "description": "Chicken Breast",
        "brand": "Kroger", "regular_price": "$6.99", "sale_price": "$4.99",
    }]


def test_check_deals_empty_result_is_not_an_error():
    """No deals found for the given terms is a normal, valid outcome -- not a failure."""
    payload = {"results": [{"term": "saffron", "success": True, "data": [_product("p1", "Saffron", 19.99)]}]}
    result = check_deals(FakeRawClient(payload), ["saffron"])
    assert result == {"deals": []}


def test_check_deals_rejects_empty_terms():
    result = check_deals(FakeRawClient({"results": []}), [])
    assert result["deals"] == []
    assert "error" in result


def test_check_deals_never_calls_add_to_cart():
    payload = {"results": [{"term": "milk", "success": True, "data": [_product("p9", "Whole Milk", 3.49, on_sale=True, sale_price=2.99)]}]}
    raw = FakeRawClient(payload)
    check_deals(raw, ["milk"])
    assert all(name != "bulk_add_to_cart" for name, _ in raw.calls)


def test_recipe_client_intercepts_check_deals_too():
    payload = {"results": [{"term": "eggs", "success": True, "data": [_product("p5", "Eggs", 2.99, on_sale=True, sale_price=1.99)]}]}
    raw = FakeRawClient(payload)
    client = KrogerRecipeClient(raw)

    result = client.call_tool("check_kroger_deals", {"terms": ["eggs"]})

    assert result["deals"] == [{
        "term": "eggs", "product_id": "p5", "description": "Eggs",
        "brand": "Kroger", "regular_price": "$2.99", "sale_price": "$1.99",
    }]
