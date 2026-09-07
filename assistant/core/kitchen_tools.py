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
    {"type": "function", "function": {
        "name": "record_purchase",
        "description": (
            "Record kitchen items just bought, from anywhere — the grocery store, a "
            "farmers market, wherever. (Kroger purchases sync in automatically; this is "
            "for everything else, or for confirming a Kroger trip in the moment.) Use "
            "whenever he says what he bought, e.g. 'I picked up 2 lbs of ground beef and "
            "a dozen eggs.'"
        ),
        "parameters": {"type": "object", "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": "number", "description": "How much/many was bought."},
                    "unit": {"type": "string", "description": "e.g. 'lb', 'dozen', 'cans'."},
                }, "required": ["name", "quantity"]},
            },
        }, "required": ["items"]},
    }},
    {"type": "function", "function": {
        "name": "update_inventory_quantity",
        "description": (
            "Correct or set how much of one kitchen item is on hand. Use whenever he "
            "reports an amount, exact or estimated — 'we're down to about half a bag of "
            "rice', 'we're out of milk', 'we have 3 eggs left', 'used another cup of "
            "flour'. Give quantity for an absolute amount (a recount) or quantity_delta "
            "for a relative change (used some, added some) — not both."
        ),
        "parameters": {"type": "object", "properties": {
            "item": {"type": "string"},
            "quantity": {"type": "number", "description": "Absolute amount now on hand."},
            "quantity_delta": {"type": "number", "description": "Relative change instead of an absolute amount."},
            "unit": {"type": "string"},
            "low_threshold": {"type": "number", "description": "Amount at/below which this counts as 'low'."},
            "notes": {"type": "string"},
        }, "required": ["item"]},
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
    {"type": "function", "function": {
        "name": "list_kitchen_inventory",
        "description": "What's currently in the kitchen, optionally filtered to what's low or out.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["have", "low", "out"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "remove_inventory_item",
        "description": "Stop tracking an item in kitchen inventory entirely (not just set it to zero).",
        "parameters": {"type": "object", "properties": {
            "item": {"type": "string"},
        }, "required": ["item"]},
    }},
    {"type": "function", "function": {
        "name": "sync_kroger_purchases",
        "description": (
            "Pull any newly-placed Kroger order into kitchen inventory right now, instead "
            "of waiting for the hourly background sync. Only ever finds something if a "
            "cart was built through Jarvis (added via chat or add_recipe_to_cart) and then "
            "actually checked out AND marked placed — Kroger's API gives no way to see a "
            "trip made independently on Kroger's own app or site, so this will usually "
            "come back empty for a normal shopping trip. Use it right after he confirms "
            "he checked out on Kroger's site following a Jarvis-built cart, or if he asks "
            "whether a Kroger order has synced yet."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "cook_recipe",
        "description": (
            "Use when he says he made/is making a saved recipe (e.g. 'I made the chili "
            "tonight'). Returns the recipe's ingredients alongside best-guess matching "
            "inventory items and quantities — it does NOT deduct anything itself. Look at "
            "what it returns, decide for yourself what quantity of which inventory item "
            "each ingredient actually used (unit conversion is your judgment call, not "
            "exact math), then call apply_recipe_deduction with your own reasoned list. "
            "Never skip straight to apply_recipe_deduction without calling this first."
        ),
        "parameters": {"type": "object", "properties": {
            "recipe_id": {"type": "integer"},
            "servings_made": {
                "type": "integer",
                "description": "If he made a different amount than the recipe's own serving size — used to hint scaling.",
            },
        }, "required": ["recipe_id"]},
    }},
    {"type": "function", "function": {
        "name": "apply_recipe_deduction",
        "description": (
            "Actually deducts inventory after cook_recipe — call this only after "
            "cook_recipe and only with your own reasoned quantities, never guessed "
            "without having called cook_recipe first. item must be one of "
            "cook_recipe's inventory_candidates' exact item names, not the recipe's own "
            "ingredient wording. Reports anything that ran short rather than silently "
            "going negative."
        ),
        "parameters": {"type": "object", "properties": {
            "recipe_id": {"type": "integer"},
            "deductions": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "item": {"type": "string", "description": "Exact inventory item name."},
                    "quantity_used": {"type": "number"},
                    "unit": {"type": "string"},
                }, "required": ["item", "quantity_used"]},
            },
        }, "required": ["recipe_id", "deductions"]},
    }},
    {"type": "function", "function": {
        "name": "list_makeable_recipes",
        "description": (
            "Which saved recipes he could start making right now, based on what's on "
            "hand. Presence-only, NOT quantity-aware — say so plainly if he asks 'do I "
            "have enough' for something specific rather than implying this checked "
            "amounts (having a single egg counts as 'have eggs' even for a recipe "
            "needing a dozen)."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "list_shopping_list",
        "description": "What's queued to buy — pending by default, or filter to what's already purchased/removed.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["pending", "purchased", "removed"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "add_to_shopping_list",
        "description": (
            "Manually queue an item to buy. Most low/out items land here on their own "
            "the moment inventory reflects it — use this for something he wants to buy "
            "that isn't already tracked in inventory, or an explicit 'add X to the "
            "shopping list'."
        ),
        "parameters": {"type": "object", "properties": {
            "item": {"type": "string"},
            "quantity_hint": {"type": "string", "description": "Free text, e.g. 'a dozen' or '2 more' — not required."},
        }, "required": ["item"]},
    }},
    {"type": "function", "function": {
        "name": "remove_from_shopping_list",
        "description": "Take an item off the shopping list without buying it (he decided against it, or it was flagged in error).",
        "parameters": {"type": "object", "properties": {
            "item": {"type": "string"},
        }, "required": ["item"]},
    }},
    {"type": "function", "function": {
        "name": "mark_shopping_list_item_purchased",
        "description": (
            "Cross an item off the shopping list because he bought it. This only updates "
            "the list itself — also call record_purchase (or update_inventory_quantity) "
            "for the actual amount bought so inventory reflects it too."
        ),
        "parameters": {"type": "object", "properties": {
            "item": {"type": "string"},
        }, "required": ["item"]},
    }},
]

KITCHEN_TOOLS = KITCHEN_ALWAYS_TOOLS + KITCHEN_GATED_TOOLS

KITCHEN_KEYWORDS = [
    "recipe", "recipes", "cook", "cooking", "cooked", "kitchen", "ingredient", "ingredients",
    "inventory", "pantry", "stock", "on hand", "kroger",
    "shopping list", "grocery list", "makeable", "made this", "made the",
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
    " You also track real kitchen inventory (quantities, not just have/low/out) with "
    "record_purchase (whenever he says what he bought, at Kroger or anywhere else — "
    "Kroger's API cannot see a trip made on Kroger's own app/site, only orders Jarvis "
    "itself built and checked out, so treat a Kroger trip he mentions the same as any "
    "other store and log it with record_purchase) and update_inventory_quantity (whenever "
    "he reports or estimates an amount — 'we're low on milk', 'we have 3 eggs left', "
    "'used a cup of flour'). Update it yourself immediately rather than just "
    "acknowledging it in conversation. list_kitchen_inventory shows what's on hand, "
    "remove_inventory_item stops tracking something entirely, and sync_kroger_purchases "
    "checks right now for a Kroger order Jarvis itself built and that has since been "
    "checked out and marked placed — a background job also does this hourly."
    " Cooking a saved recipe is always two calls, never one: cook_recipe first (shows "
    "the recipe's ingredients plus best-guess matching inventory — decides nothing), "
    "then apply_recipe_deduction with your own reasoned quantities once you've looked at "
    "what it returned. list_makeable_recipes says what he could start making right now "
    "from what's on hand — presence-only, not quantity-aware, so say that plainly if he "
    "asks whether he has 'enough' of something specific. A shopping list also exists: "
    "most items land on it themselves the moment inventory goes low or out, but "
    "add_to_shopping_list/remove_from_shopping_list/list_shopping_list handle it "
    "directly, and mark_shopping_list_item_purchased crosses something off once he's "
    "bought it — pair that with record_purchase for the actual amount, since marking "
    "purchased only touches the list, not inventory."
)


def dispatch(db_path: str, owner_user_id: int, name: str, arguments: dict, kroger_mcp_client=None) -> dict:
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

    if name == "record_purchase":
        updated = []
        for entry in arguments.get("items", []):
            if not entry.get("name"):
                continue
            r = kitchen_db.upsert_inventory_item(
                db_path, owner_user_id, entry["name"],
                quantity_delta=entry.get("quantity", 0), unit=entry.get("unit"),
                reason="purchase_manual",
            )
            updated.append({"item": r["item"], "quantity": r["quantity"], "unit": r["unit"]})
        return {"ok": True, "updated": updated}

    if name == "update_inventory_quantity":
        r = kitchen_db.upsert_inventory_item(
            db_path, owner_user_id, arguments["item"],
            quantity_set=arguments.get("quantity"), quantity_delta=arguments.get("quantity_delta"),
            unit=arguments.get("unit"), low_threshold=arguments.get("low_threshold"),
            notes=arguments.get("notes"), reason="manual_adjust",
        )
        return {"ok": True, "item": r["item"], "quantity": r["quantity"], "unit": r["unit"], "status": r["status"]}

    if name == "list_kitchen_inventory":
        return {"inventory": kitchen_db.list_inventory(db_path, owner_user_id, arguments.get("status"))}

    if name == "remove_inventory_item":
        existing = kitchen_db.get_inventory_item(db_path, owner_user_id, arguments["item"])
        if existing is None:
            return {"error": "item not found in inventory"}
        ok = kitchen_db.delete_inventory_item(db_path, owner_user_id, existing["id"])
        return {"ok": ok}

    if name == "sync_kroger_purchases":
        if kroger_mcp_client is None:
            return {"error": "Kroger is not configured"}
        return kitchen_db.sync_kroger_orders(kroger_mcp_client, db_path, owner_user_id)

    if name == "cook_recipe":
        return kitchen_db.cook_recipe(
            db_path, owner_user_id, arguments["recipe_id"], arguments.get("servings_made"))

    if name == "apply_recipe_deduction":
        return kitchen_db.apply_recipe_deduction(
            db_path, owner_user_id, arguments["recipe_id"], arguments.get("deductions", []))

    if name == "list_makeable_recipes":
        return kitchen_db.list_makeable_recipes(db_path, owner_user_id)

    if name == "list_shopping_list":
        return {"shopping_list": kitchen_db.list_shopping_list(db_path, owner_user_id, arguments.get("status", "pending"))}

    if name == "add_to_shopping_list":
        return {"ok": True, "item": kitchen_db.add_to_shopping_list(
            db_path, owner_user_id, arguments["item"], arguments.get("quantity_hint"))}

    if name == "remove_from_shopping_list":
        ok = kitchen_db.remove_from_shopping_list(db_path, owner_user_id, arguments["item"])
        return {"ok": ok} if ok else {"error": "item not found (pending) on the shopping list"}

    if name == "mark_shopping_list_item_purchased":
        ok = kitchen_db.mark_shopping_list_item_purchased(db_path, owner_user_id, arguments["item"])
        return {"ok": ok} if ok else {"error": "item not found (pending) on the shopping list"}

    return {"error": f"unknown kitchen tool {name}"}
