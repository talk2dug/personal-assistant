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
from . import kitchen_db, meal_plan_db

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
            "for a relative change (used some, added some) — not both. If he's just "
            "telling you to start tracking something new WITHOUT giving any amount, "
            "call this with only item (and unit, if he gave one) — omit both quantity "
            "fields entirely rather than guessing a number; a new item with nothing "
            "specified is automatically marked fully stocked."
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
    {"type": "function", "function": {
        "name": "display_recipe",
        "description": (
            "Show a saved recipe's ingredients and steps on a kitchen screen — use "
            "whenever he asks to see, pull up, or display a recipe while cooking, from "
            "anywhere in the house (not just standing at the screen itself). Stays up "
            "until he taps it away, so don't worry about timing this to when he'll "
            "actually look."
        ),
        "parameters": {"type": "object", "properties": {
            "recipe_id": {"type": "integer"},
            "location": {
                "type": "string",
                "description": "Which screen — defaults to 'kitchen', the only one wired up today.",
            },
        }, "required": ["recipe_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_pay_period",
        "description": (
            "Find the pay period(s) around today, for meal planning ('plan meals for the "
            "days between paydays'). Returns both the period today falls inside "
            "(is_current: true) and the next upcoming one, since which he means when he "
            "just says 'let's plan meals' is genuinely ambiguous mid-cycle — ask him if "
            "it isn't obvious from context, don't just assume."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "start_meal_plan",
        "description": (
            "Start a new meal plan for a date range (normally one pay period, from "
            "get_pay_period). Creates it as a draft — keep adding meals with "
            "add_meal_plan_entry, then call finalize_meal_plan once he's happy with it."
        ),
        "parameters": {"type": "object", "properties": {
            "period_start": {"type": "string", "description": "ISO date, inclusive."},
            "period_end": {"type": "string", "description": "ISO date — the next payday."},
            "max_deliveries": {
                "type": "integer",
                "description": "Grocery deliveries allowed this period — defaults to 2 (one main trip, one fresh top-off) unless he says otherwise.",
            },
        }, "required": ["period_start", "period_end"]},
    }},
    {"type": "function", "function": {
        "name": "add_meal_plan_entry",
        "description": (
            "Plan one meal on one date. Calling this again for the same plan/date/meal_type "
            "replaces whatever was already planned there, rather than duplicating it — use "
            "that freely as the conversation changes its mind about a night. title is "
            "required even when recipe_id is given (denormalized for quick display); for "
            "something with no saved recipe ('order pizza', 'leftovers'), give title alone."
        ),
        "parameters": {"type": "object", "properties": {
            "meal_plan_id": {"type": "integer"},
            "plan_date": {"type": "string", "description": "ISO date."},
            "meal_type": {"type": "string", "enum": ["breakfast", "lunch", "dinner"]},
            "title": {"type": "string"},
            "recipe_id": {"type": "integer", "description": "A saved recipe, if this meal is one."},
            "servings_planned": {"type": "integer"},
            "source": {
                "type": "string",
                "enum": ["fresh", "frozen_substitute", "frozen_premade", "batch_frozen", "leftover", "eating_out"],
                "description": "Defaults to 'fresh'. Use frozen_substitute when swapping a fresh ingredient for frozen to cut delivery trips — always propose that swap and get his OK in conversation first, never set it silently. Use batch_frozen for a meal pulled from a logged batch-cook session (see log_batch_cook_session/list_batch_frozen_inventory) — pair it with batch_session_id.",
            },
            "batch_session_id": {
                "type": "integer",
                "description": "Only meaningful when source='batch_frozen' — the batch_cook_sessions entry this meal is pulled from (from list_batch_frozen_inventory). Consumes exactly one portion from that session; ignored for any other source.",
            },
            "notes": {"type": "string"},
        }, "required": ["meal_plan_id", "plan_date", "meal_type", "title"]},
    }},
    {"type": "function", "function": {
        "name": "list_meal_plan",
        "description": "Show a meal plan and everything planned in it so far. Omit meal_plan_id for the current draft/active plan.",
        "parameters": {"type": "object", "properties": {
            "meal_plan_id": {"type": "integer"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "remove_meal_plan_entry",
        "description": "Remove one planned meal (he changed his mind about that night, or it was a mistake).",
        "parameters": {"type": "object", "properties": {
            "entry_id": {"type": "integer"},
        }, "required": ["entry_id"]},
    }},
    {"type": "function", "function": {
        "name": "finalize_meal_plan",
        "description": (
            "Lock in a meal plan once he's happy with it — call this when the planning "
            "conversation reaches a clear 'that's the plan.' Marks it active rather than "
            "draft; later steps (the shopping list, shopping-day scheduling) key off a plan "
            "actually being finalized."
        ),
        "parameters": {"type": "object", "properties": {
            "meal_plan_id": {"type": "integer"},
        }, "required": ["meal_plan_id"]},
    }},
    {"type": "function", "function": {
        "name": "generate_meal_plan_shopping_list",
        "description": (
            "Gather what a meal plan's recipes actually need, alongside best-guess "
            "matching inventory rows — decides nothing itself, just gathers data. Look at "
            "what it returns and reason out how much of each item actually still needs "
            "buying given what's already on hand (an ingredient used by two different "
            "meals only needs buying once, combined), then call "
            "save_meal_plan_shopping_items with your own conclusions. Never skip straight "
            "to save_meal_plan_shopping_items without calling this first."
        ),
        "parameters": {"type": "object", "properties": {
            "meal_plan_id": {"type": "integer"},
        }, "required": ["meal_plan_id"]},
    }},
    {"type": "function", "function": {
        "name": "save_meal_plan_shopping_items",
        "description": (
            "Save your own reasoned shopping list for a plan, after calling "
            "generate_meal_plan_shopping_list first. Replaces any previously saved list "
            "for this plan entirely — call it again with the full list any time the plan "
            "changes, not just the new/changed items."
        ),
        "parameters": {"type": "object", "properties": {
            "meal_plan_id": {"type": "integer"},
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "item": {"type": "string"},
                    "quantity_needed": {"type": "string", "description": "Free text, e.g. '4 cups total across both dinners'."},
                    "quantity_on_hand": {"type": "string", "description": "Free text, e.g. 'about 1 cup'."},
                    "quantity_to_buy": {"type": "string", "description": "Free text, e.g. '3 cups' or '1 bag'."},
                    "category": {"type": "string", "enum": ["fresh_produce", "frozen", "pantry", "dairy", "meat", "other"]},
                }, "required": ["item"]},
            },
        }, "required": ["meal_plan_id", "items"]},
    }},
    {"type": "function", "function": {
        "name": "match_meal_plan_items_to_kroger",
        "description": (
            "Find real Kroger products matching a saved shopping list's items, including "
            "whether each is currently on sale — this only searches, it does NOT add "
            "anything to the cart. Read the matches back to him, flag anything unmatched "
            "or an obvious size mismatch, and only call bulk_add_to_cart yourself after he "
            "confirms which ones to actually buy."
        ),
        "parameters": {"type": "object", "properties": {
            "meal_plan_id": {"type": "integer"},
        }, "required": ["meal_plan_id"]},
    }},
    {"type": "function", "function": {
        "name": "propose_shopping_days",
        "description": (
            "Gathers a finalized plan's entries (date/meal/source) plus its max_deliveries "
            "cap — decides nothing itself. Look at which entries are 'fresh' (need buying "
            "close to their own date) versus frozen/pantry-stable/leftover/eating-out "
            "(no date pressure), then reason out and present up to max_deliveries shopping "
            "dates spanning the pay period for him to confirm in chat. Never exceed "
            "max_deliveries. This never schedules or saves anything on its own — the "
            "shopping days you propose only exist once you've said them to him."
        ),
        "parameters": {"type": "object", "properties": {
            "meal_plan_id": {"type": "integer"},
        }, "required": ["meal_plan_id"]},
    }},
    {"type": "function", "function": {
        "name": "schedule_freezer_pulls",
        "description": (
            "Once a plan is finalized, creates a real reminder the evening before each "
            "freezer-sourced entry's date ('pull X from the freezer tonight for tomorrow's "
            "Y') for every entry whose source is frozen_substitute, frozen_premade, or "
            "batch_frozen. Safe to call again after the plan changes — entries that "
            "already have a reminder are left alone, only new/changed freezer entries get "
            "one."
        ),
        "parameters": {"type": "object", "properties": {
            "meal_plan_id": {"type": "integer"},
        }, "required": ["meal_plan_id"]},
    }},
    {"type": "function", "function": {
        "name": "log_batch_cook_session",
        "description": (
            "Log a batch-cook-and-freeze session — he made a big batch of something and "
            "vacuum-packed it into portions for lazy nights/workweek lunches. Use whenever "
            "he mentions batch cooking, meal-prepping, or freezing portions of something "
            "he just made. servings_made is the real number of freezer-ready portions it "
            "made (not people served) — ask if it's not obvious from what he said."
        ),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "e.g. 'Turkey chili' — required even if recipe_id is given."},
            "servings_made": {"type": "integer", "description": "How many freezer portions this session produced."},
            "cooked_date": {"type": "string", "description": "ISO date. Defaults to today if omitted."},
            "recipe_id": {"type": "integer", "description": "A saved recipe, if this batch was one."},
            "notes": {"type": "string"},
        }, "required": ["title", "servings_made"]},
    }},
    {"type": "function", "function": {
        "name": "list_batch_frozen_inventory",
        "description": (
            "What's currently in the freezer from past batch-cook sessions, and how many "
            "portions of each are left. Use this before planning a batch_frozen meal (to "
            "see what's actually available to pull from) or whenever he asks what's in "
            "the freezer."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
]

KITCHEN_TOOLS = KITCHEN_ALWAYS_TOOLS + KITCHEN_GATED_TOOLS

KITCHEN_KEYWORDS = [
    "recipe", "recipes", "cook", "cooking", "cooked", "kitchen", "ingredient", "ingredients",
    "inventory", "pantry", "stock", "on hand", "kroger",
    "shopping list", "grocery list", "makeable", "made this", "made the",
    "display", "show me the", "pull up",
    "meal plan", "meal planning", "plan meals", "pay period", "payday",
]

# Which physical screen a location name resolves to. Not a generic location registry --
# laptop1 (a Dell Latitude running jarvis-device.service as the kitchen voice terminal,
# confirmed during the show_camera work) is the only kitchen screen that exists today, so
# a hardcoded map is honest about the current setup rather than building generality for
# devices that don't exist yet.
KITCHEN_DEVICE_MAP = {"kitchen": "laptop1"}


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
    " display_recipe puts a saved recipe's ingredients and steps up on the kitchen "
    "screen — use it whenever he wants to see a recipe while cooking, whether he's "
    "standing at that screen or asking from anywhere else in the house."
    " Meal planning is meant to be an actual back-and-forth conversation, not a form to "
    "fill in silently — talk it through with him rather than dumping a finished plan. "
    "Start with get_pay_period so the date range is grounded in his real pay schedule "
    "rather than a guess, confirming which period he means if it's ambiguous. Use "
    "check_kroger_deals on the specific ingredients/categories you're actually "
    "considering to steer toward what's cheap right now — it can only answer for terms "
    "you give it, never browse a general deals list, so don't imply you checked "
    "anything broader than that. He has ADHD: favor low-prep, low-decision-fatigue "
    "meals (few steps, minimal simultaneous multitasking, things that reheat or batch "
    "well) over anything demanding a lot of attention-switching, and ask about his "
    "energy/appetite patterns rather than assuming — frozen convenience meals (pizza, "
    "fish sticks, fries) are entirely legitimate plan entries, not a fallback to "
    "apologize for. Build the plan with start_meal_plan/add_meal_plan_entry/"
    "list_meal_plan/remove_meal_plan_entry as you go, and only call finalize_meal_plan "
    "once he's actually said the plan looks good — don't finalize on your own judgment "
    "that it's probably done. Once finalized, generate_meal_plan_shopping_list gathers "
    "what the plan's recipes need against what's already on hand (decides nothing "
    "itself); reason out real quantities to buy from what it returns — combining an "
    "ingredient used by more than one meal into one line rather than buying it twice — "
    "and save your conclusions with save_meal_plan_shopping_items. "
    "match_meal_plan_items_to_kroger then finds real products (and sale status) for that "
    "saved list; read the matches back to him and only call bulk_add_to_cart yourself "
    "once he's confirmed which ones to actually buy."
    " Once a plan is finalized, propose_shopping_days gathers its entries so you can "
    "reason out and present up to max_deliveries shopping dates (fresh entries need "
    "buying close to their own date, frozen/pantry-stable/leftover/eating-out entries "
    "don't) -- present the dates in chat, it saves nothing on its own. "
    "schedule_freezer_pulls then creates a real reminder the evening before each "
    "frozen_substitute/frozen_premade/batch_frozen entry so he's told what to pull that "
    "night -- safe to call again any time the plan changes, already-covered entries are "
    "left alone. For batch-cooked freezer meals: log_batch_cook_session whenever he "
    "mentions batch cooking or freezing portions of something (capture the real number "
    "of freezer portions it made), list_batch_frozen_inventory shows what's left before "
    "planning a batch_frozen meal, and add_meal_plan_entry with source='batch_frozen' "
    "plus batch_session_id consumes one portion from that session automatically."
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

    if name == "display_recipe":
        recipe = kitchen_db.get_recipe(db_path, owner_user_id, arguments["recipe_id"])
        if recipe is None:
            return {"error": "recipe not found"}
        location = (arguments.get("location") or "kitchen").strip().lower()
        device_id = KITCHEN_DEVICE_MAP.get(location)
        if device_id is None:
            return {"error": f"no kitchen screen known for '{location}'"}
        kitchen_db.set_pending_recipe_view(db_path, device_id, {
            "recipe_id": recipe["id"], "title": recipe["title"], "servings": recipe.get("servings"),
            "ingredients": recipe["ingredients"], "steps": recipe["steps"],
        })
        return {"ok": True, "device_id": device_id, "title": recipe["title"]}

    if name == "get_pay_period":
        return {"pay_periods": meal_plan_db.get_pay_periods(db_path, owner_user_id)}

    if name == "start_meal_plan":
        meal_plan_id = meal_plan_db.create_meal_plan(
            db_path, owner_user_id, arguments["period_start"], arguments["period_end"],
            max_deliveries=arguments.get("max_deliveries", 2), notes=arguments.get("notes"))
        return {"ok": True, "meal_plan_id": meal_plan_id}

    if name == "add_meal_plan_entry":
        entry = meal_plan_db.add_meal_plan_entry(
            db_path, owner_user_id, arguments["meal_plan_id"], arguments["plan_date"], arguments["meal_type"],
            arguments["title"], recipe_id=arguments.get("recipe_id"),
            servings_planned=arguments.get("servings_planned"), source=arguments.get("source", "fresh"),
            batch_session_id=arguments.get("batch_session_id"), notes=arguments.get("notes"))
        if entry is None:
            return {"error": "meal plan not found"}
        return {"ok": True, "entry": entry}

    if name == "list_meal_plan":
        result = meal_plan_db.get_meal_plan_with_entries(db_path, owner_user_id, arguments.get("meal_plan_id"))
        if result is None:
            return {"error": "no meal plan found"}
        return result

    if name == "remove_meal_plan_entry":
        ok = meal_plan_db.remove_meal_plan_entry(db_path, owner_user_id, arguments["entry_id"])
        return {"ok": ok} if ok else {"error": "entry not found"}

    if name == "finalize_meal_plan":
        ok = meal_plan_db.finalize_meal_plan(db_path, owner_user_id, arguments["meal_plan_id"])
        return {"ok": ok} if ok else {"error": "meal plan not found"}

    if name == "generate_meal_plan_shopping_list":
        result = meal_plan_db.gather_meal_plan_ingredients(db_path, owner_user_id, arguments["meal_plan_id"])
        if result is None:
            return {"error": "meal plan not found"}
        return result

    if name == "save_meal_plan_shopping_items":
        result = meal_plan_db.save_meal_plan_shopping_items(
            db_path, owner_user_id, arguments["meal_plan_id"], arguments.get("items", []))
        if result is None:
            return {"error": "meal plan not found"}
        return result

    if name == "match_meal_plan_items_to_kroger":
        if kroger_mcp_client is None:
            return {"error": "Kroger is not configured"}
        result = meal_plan_db.match_meal_plan_items_to_kroger(
            db_path, owner_user_id, arguments["meal_plan_id"], kroger_mcp_client)
        if result is None:
            return {"error": "meal plan not found"}
        return result

    if name == "propose_shopping_days":
        result = meal_plan_db.propose_shopping_days(db_path, owner_user_id, arguments["meal_plan_id"])
        if result is None:
            return {"error": "meal plan not found"}
        return result

    if name == "schedule_freezer_pulls":
        result = meal_plan_db.schedule_freezer_pulls(db_path, owner_user_id, arguments["meal_plan_id"])
        if result is None:
            return {"error": "meal plan not found"}
        return result

    if name == "log_batch_cook_session":
        session = meal_plan_db.log_batch_cook_session(
            db_path, owner_user_id, arguments["title"], arguments["servings_made"],
            cooked_date=arguments.get("cooked_date"), recipe_id=arguments.get("recipe_id"),
            notes=arguments.get("notes"))
        return {"ok": True, "session": session}

    if name == "list_batch_frozen_inventory":
        return {"batch_frozen_inventory": meal_plan_db.list_batch_cook_sessions(db_path, owner_user_id)}

    return {"error": f"unknown kitchen tool {name}"}
