"""Chat-facing tools for the recipe catalog and kitchen inventory — a sibling of
personal_tools.py the same way personal_tools.py is a sibling of business_tools.py.
Dispatched through PersonalClient (personal_tools.py) rather than a new Context class:
this is still all owner-only, local-database-only, spends-no-money personal-life data,
same reasoning that already keeps the rest of PersonalClient ungated.

Split into two tool lists rather than one, unlike most of PERSONAL_TOOLS: save_recipe is
capture-shaped (must be available every turn so a recipe mentioned in passing actually
gets saved), but PERSONAL_TOOLS is already 21 schemas and always-on by design -- adding
this feature's full tool set on top of that would reintroduce the exact tool-count-
overload problem this codebase has already hit and fixed twice (Era's 51-tool overload,
the reason Kroger/CCXT/Airbnb/Ticketmaster/Recipe API are all keyword-routed). So only
the true "capture what he just said" tools are unconditional; recipe lookup/editing is
keyword-gated the same way _select_kroger_tools gates Kroger's larger catalog.
"""
from . import kitchen_db

KITCHEN_ALWAYS_TOOLS = [
    {"type": "function", "function": {
        "name": "save_recipe",
        "description": (
            "Save a recipe to the owner's own catalog — title, ingredients, and steps. Use "
            "this whenever he describes, pastes, or dictates a recipe he wants kept, rather "
            "than just replying with it and letting it evaporate."
        ),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "ingredients": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": "string", "description": "e.g. '2', '1/2', '2-3'."},
                    "unit": {"type": "string", "description": "e.g. 'cups', 'lb', 'cloves'."},
                    "notes": {"type": "string"},
                }, "required": ["name"]},
            },
            "steps": {"type": "array", "items": {"type": "string"}, "description": "In order."},
            "servings": {"type": "integer"},
            "notes": {"type": "string"},
        }, "required": ["title", "ingredients", "steps"]},
    }},
]

KITCHEN_GATED_TOOLS = [
    {"type": "function", "function": {
        "name": "list_recipes",
        "description": "List the owner's saved recipes, optionally filtered by a title search.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Optional text to search titles for."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_recipe",
        "description": "Fetch one saved recipe's full ingredients and steps by id.",
        "parameters": {"type": "object", "properties": {
            "recipe_id": {"type": "integer"},
        }, "required": ["recipe_id"]},
    }},
    {"type": "function", "function": {
        "name": "update_recipe",
        "description": "Edit a saved recipe's title, servings, ingredients, steps, or notes.",
        "parameters": {"type": "object", "properties": {
            "recipe_id": {"type": "integer"},
            "title": {"type": "string"},
            "servings": {"type": "integer"},
            "ingredients": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"}, "quantity": {"type": "string"},
                    "unit": {"type": "string"}, "notes": {"type": "string"},
                }, "required": ["name"]},
            },
            "steps": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"},
        }, "required": ["recipe_id"]},
    }},
    {"type": "function", "function": {
        "name": "delete_recipe",
        "description": "Remove a saved recipe from the catalog.",
        "parameters": {"type": "object", "properties": {
            "recipe_id": {"type": "integer"},
        }, "required": ["recipe_id"]},
    }},
]

KITCHEN_TOOLS = KITCHEN_ALWAYS_TOOLS + KITCHEN_GATED_TOOLS

KITCHEN_KEYWORDS = [
    "recipe", "recipes", "cook", "cooking", "kitchen", "ingredient", "ingredients",
]


def _select_kitchen_gated_tools(user_text: str) -> list[dict]:
    text = user_text.lower()
    if not any(kw in text for kw in KITCHEN_KEYWORDS):
        return []
    return KITCHEN_GATED_TOOLS


KITCHEN_SYSTEM_NOTE = (
    " You also keep the owner's recipe catalog: save_recipe whenever he describes, pastes, "
    "or dictates a recipe he wants kept, and list_recipes/get_recipe/update_recipe/"
    "delete_recipe to browse, read, edit, or remove what's saved. Ingredients are "
    "structured (name/quantity/unit), and steps are an ordered list of plain strings — "
    "keep the wording of each step close to how he described it rather than rewriting it."
)


def dispatch(db_path: str, owner_user_id: int, name: str, arguments: dict) -> dict:
    if name == "save_recipe":
        recipe_id = kitchen_db.create_recipe(
            db_path, owner_user_id, arguments["title"], arguments["ingredients"], arguments["steps"],
            servings=arguments.get("servings"), notes=arguments.get("notes"))
        return {"ok": True, "recipe_id": recipe_id}
    if name == "list_recipes":
        return {"recipes": kitchen_db.list_recipes(db_path, owner_user_id, arguments.get("query"))}
    if name == "get_recipe":
        recipe = kitchen_db.get_recipe(db_path, owner_user_id, arguments["recipe_id"])
        if recipe is None:
            return {"error": "recipe not found"}
        return {"recipe": recipe}
    if name == "update_recipe":
        ok = kitchen_db.update_recipe(
            db_path, owner_user_id, arguments["recipe_id"],
            title=arguments.get("title"), servings=arguments.get("servings"),
            ingredients=arguments.get("ingredients"), steps=arguments.get("steps"),
            notes=arguments.get("notes"))
        return {"ok": ok}
    if name == "delete_recipe":
        ok = kitchen_db.delete_recipe(db_path, owner_user_id, arguments["recipe_id"])
        return {"ok": ok}
    return {"error": f"unknown kitchen tool {name}"}
