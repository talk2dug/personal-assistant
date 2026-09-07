"""Recipe-to-cart: turns "I want to make chili" into matched real Kroger products,
without ever writing to the real cart itself.

Not part of kroger-mcp's own catalog -- this is a synthetic tool Jarvis adds on top of
it (see setup.py's build_kroger_context, which wraps the raw stdio client in
_KrogerRecipeClient and appends RECIPE_TOOL_SCHEMA to the discovered tool list) because
matching a recipe's ingredient list against real products is a judgment call an LLM
should make, not something the vendored MCP server does.

Deliberately split into a search step (this module) and a write step (the existing,
unmodified bulk_add_to_cart, already gated in KROGER_SENSITIVE_TOOLS): "2 cups of flour"
becoming "1 five-pound bag" is inherently lossy, so the owner needs to see and confirm
the actual matches before anything real is staged -- exactly the same reasoning that
keeps every other Kroger cart write behind a confirmation.
"""
import json

RECIPE_TOOL_NAME = "add_recipe_to_cart"

RECIPE_TOOL_SCHEMA = {"type": "function", "function": {
    "name": RECIPE_TOOL_NAME,
    "description": (
        "Find real Kroger products matching a dish's ingredients, as a first step "
        "toward adding them to the cart. This only searches -- it does NOT add "
        "anything to the cart. Supply the ingredient list yourself from your own "
        "knowledge of the dish (there is no recipe database here). After this "
        "returns matches, read them back to the owner in plain terms — name any "
        "ingredient with no good match or where the package size clearly doesn't "
        "fit the recipe (e.g. a whole bag of flour for one tablespoon) — and only "
        "then, if he confirms, call bulk_add_to_cart yourself with the confirmed "
        "product_ids. Never add anything without him seeing the matches first."
    ),
    "parameters": {"type": "object", "properties": {
        "dish": {"type": "string", "description": "What he wants to make, e.g. 'chili'."},
        "servings": {"type": "integer"},
        "ingredients": {
            "type": "array", "items": {"type": "string"},
            "description": "Plain ingredient names to search for, e.g. ['ground beef', 'kidney beans', 'chili powder'].",
        },
    }, "required": ["dish", "ingredients"]},
}}


def _parse_content(result: dict) -> dict:
    """StdioMCPClient.call_tool returns {"is_error", "content": [json_text, ...]} --
    same shape every other integration's raw tool results come back in (see
    scheduler.py's _era_payload for the same unwrapping)."""
    if result.get("is_error"):
        raise RuntimeError("; ".join(result.get("content") or ["kroger tool call failed"]))
    content = result.get("content") or []
    if not content:
        return {}
    return json.loads(content[0])


def match_ingredients(raw_client, ingredients: list[str], location_id: str | None = None) -> dict:
    """Runs one bulk_search_products call (not one search per ingredient -- the
    non-agentic backend caps tool hops, and a real recipe has 8-10 ingredients) and
    reduces each result to its best match plus an explicit no-match flag."""
    ingredients = [i.strip() for i in ingredients if i and i.strip()][:25]
    if not ingredients:
        return {"matches": [], "error": "no ingredients provided"}

    searches = [{"term": ing, "limit": 3} for ing in ingredients]
    args = {"searches": searches}
    if location_id:
        args["location_id"] = location_id

    try:
        payload = _parse_content(raw_client.call_tool("bulk_search_products", args))
    except Exception as e:
        return {"matches": [], "error": f"search failed: {e}"}

    results = payload.get("results") or []
    matches = []
    for ingredient, result in zip(ingredients, results):
        products = result.get("data") or []
        best = products[0] if products else None
        item = matches.append
        if best is None:
            item({"ingredient": ingredient, "matched": False})
            continue
        pricing = best.get("pricing") or {}
        size = (best.get("item") or {}).get("size")
        item({
            "ingredient": ingredient,
            "matched": True,
            "product_id": best.get("product_id"),
            "description": best.get("description"),
            "brand": best.get("brand"),
            "size": size,
            "price": pricing.get("formatted_sale") or pricing.get("formatted_regular"),
        })

    return {
        "dish_note": (
            "These are proposed matches only — nothing has been added to the cart. "
            "Read them back to the owner, flag unmatched ingredients and any obvious "
            "size mismatch, then only call bulk_add_to_cart after he confirms."
        ),
        "matches": matches,
    }


DEAL_TOOL_NAME = "check_kroger_deals"

DEAL_TOOL_SCHEMA = {"type": "function", "function": {
    "name": DEAL_TOOL_NAME,
    "description": (
        "Check whether specific grocery items are currently on sale at Kroger — use this "
        "while meal planning to steer toward what's actually cheap right now. There is no "
        "'what's on sale this week' browse in Kroger's API: this can only answer for terms "
        "you actually supply (e.g. 'chicken breast', 'ground beef', 'broccoli'), never a "
        "general deals list, so feed it terms relevant to what he might buy rather than a "
        "bare/generic query. An empty result means nothing you asked about happens to be on "
        "sale right now — that's a normal outcome, not a failure, and is worth saying "
        "plainly rather than implying you checked something broader than you did."
    ),
    "parameters": {"type": "object", "properties": {
        "terms": {
            "type": "array", "items": {"type": "string"},
            "description": "Grocery search terms to check, e.g. ['chicken breast', 'broccoli', 'pasta'].",
        },
    }, "required": ["terms"]},
}}


def check_deals(raw_client, terms: list[str], location_id: str | None = None) -> dict:
    """One bulk_search_products call across the given terms, returns only the results
    where pricing.on_sale is true. Reads each result's own "term" field back (rather than
    zip-ing positionally against the input, the way match_ingredients above does) since
    bulk_search_products already echoes it per-result -- more robust if the server ever
    reorders or drops a failed search."""
    terms = [t.strip() for t in terms if t and t.strip()][:25]
    if not terms:
        return {"deals": [], "error": "no search terms provided"}

    searches = [{"term": t, "limit": 5} for t in terms]
    args = {"searches": searches}
    if location_id:
        args["location_id"] = location_id

    try:
        payload = _parse_content(raw_client.call_tool("bulk_search_products", args))
    except Exception as e:
        return {"deals": [], "error": f"search failed: {e}"}

    deals = []
    for result in payload.get("results") or []:
        for product in result.get("data") or []:
            pricing = product.get("pricing") or {}
            if not pricing.get("on_sale"):
                continue
            deals.append({
                "term": result.get("term"),
                "product_id": product.get("product_id"),
                "description": product.get("description"),
                "brand": product.get("brand"),
                "regular_price": pricing.get("formatted_regular"),
                "sale_price": pricing.get("formatted_sale"),
            })

    return {"deals": deals}


class KrogerRecipeClient:
    """Wraps the real Kroger stdio client to add the two synthetic tools above
    (add_recipe_to_cart, check_kroger_deals). Every other tool name passes straight
    through untouched."""

    def __init__(self, raw):
        self._raw = raw

    def list_tools(self):
        return self._raw.list_tools()

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name == RECIPE_TOOL_NAME:
            result = match_ingredients(self._raw, arguments.get("ingredients") or [])
            result["dish"] = arguments.get("dish")
            if arguments.get("servings"):
                result["servings"] = arguments["servings"]
            return result
        if name == DEAL_TOOL_NAME:
            return check_deals(self._raw, arguments.get("terms") or [])
        return self._raw.call_tool(name, arguments)
