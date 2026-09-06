"""Chat-facing tools for the business side — how the owner directs his second in command.

Schemas live here rather than in engine.py (where Era/mail/Obsidian/HA schemas sit) purely
for size: engine.py is already ~900 lines and this is the largest tool group yet. The
client exposes the same call_tool(name, arguments) shape as every other integration, so
engine's dispatch treats it identically.

Nothing here is gated behind pending_actions. These tools write to Jarvis's own database —
notes, tasks, leads, expense rows. Nothing leaves the machine, nothing spends money, and
nothing touches the physical world, which is the line the confirmation gate exists to
guard. The agents that DO reach the internet run unattended on a timer and deliberately
have no tools at all (see agents.py).
"""
import json
import threading
from datetime import date, timedelta

from . import agents, business_db, market_data, paper_trading, staff

BUSINESS_TOOLS = [
    {"type": "function", "function": {
        "name": "business_status",
        "description": (
            "Overview of the business: active projects, open tasks, new market leads and "
            "trend ideas, recent agent activity, spend this month, and anything low on stock. "
            "Use this for broad questions like 'where are we at' or 'what's going on with the business'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "list_projects",
        "description": "List business projects (things being built or worked toward).",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["active", "paused", "done", "dropped"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_project",
        "description": "Start tracking a new business project. Use when the owner says what he wants to build or take on.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "goal": {"type": "string", "description": "What finishing this looks like."},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "update_project",
        "description": "Change a project's name, goal, or status.",
        "parameters": {"type": "object", "properties": {
            "project_id": {"type": "integer"},
            "name": {"type": "string"},
            "goal": {"type": "string"},
            "status": {"type": "string", "enum": ["active", "paused", "done", "dropped"]},
        }, "required": ["project_id"]},
    }},
    {"type": "function", "function": {
        "name": "list_tasks",
        "description": "List business tasks, optionally filtered by status or project.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["open", "doing", "done", "dropped"]},
            "project_id": {"type": "integer"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_task",
        "description": (
            "Add a business to-do. Use for work items on the business, not personal reminders "
            "(those are add_reminder). Create these proactively when the owner describes "
            "something that needs doing."
        ),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "project_id": {"type": "integer", "description": "Optional project to file it under."},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "due_at": {"type": "string", "description": "Optional local ISO 8601 date/time."},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "update_task",
        "description": "Update a task — mark it done, change priority, move it, reword it.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "text": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "doing", "done", "dropped"]},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "project_id": {"type": "integer"},
        }, "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "request_research",
        "description": (
            "Queue a research job for the background agent, which searches the web and writes "
            "a practical brief. Use this when the owner asks you to look into something that "
            "deserves real digging rather than a quick answer — suppliers, pricing, "
            "regulations, a market, a competitor. Tell him you've put it in hand and that "
            "you'll report back; results arrive in the daily briefing or on request."
        ),
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "Short label, e.g. 'DTF printer suppliers'."},
            "question": {"type": "string", "description": "The specific thing to find out."},
            "project_id": {"type": "integer"},
        }, "required": ["topic"]},
    }},
    {"type": "function", "function": {
        "name": "list_research",
        "description": "Recent research jobs and their findings, including anything still queued.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Default 5."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "list_market_leads",
        "description": "Vendor markets, craft fairs and pop-ups the market agent has found, best fit first.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["new", "interested", "applied", "booked", "rejected", "passed"]},
            "min_score": {"type": "integer", "description": "Only leads scoring at least this (0-100)."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "set_market_lead_status",
        "description": "Mark where a market stands — interested, applied, booked, or passed on.",
        "parameters": {"type": "object", "properties": {
            "lead_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["new", "interested", "applied", "booked", "rejected", "passed"]},
        }, "required": ["lead_id", "status"]},
    }},
    {"type": "function", "function": {
        "name": "list_trend_leads",
        "description": "Product/design ideas the trend agent has surfaced, highest potential first.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["new", "making", "made", "passed"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "set_trend_lead_status",
        "description": "Mark a product idea as being made, made, or passed on.",
        "parameters": {"type": "object", "properties": {
            "lead_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["new", "making", "made", "passed"]},
        }, "required": ["lead_id", "status"]},
    }},
    {"type": "function", "function": {
        "name": "run_business_agent",
        "description": (
            "Kick off a background scan immediately instead of waiting for its schedule. "
            "Takes several minutes and runs in the background — say you've set it going and "
            "that you'll report what it finds; do not claim results you don't have yet."
        ),
        "parameters": {"type": "object", "properties": {
            "agent": {"type": "string", "enum": [
                "market_finder", "trend_scout", "research",
                "product_creator", "art_director", "store_manager", "social_director",
            ]},
        }, "required": ["agent"]},
    }},
    {"type": "function", "function": {
        "name": "list_product_concepts",
        "description": "Product ideas the Product Creator has proposed, and where each one stands.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string",
                       "enum": ["proposed", "approved", "in_production", "live", "retired", "rejected"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "set_concept_status",
        "description": (
            "Approve or reject a product concept. Approving it is what releases the Art "
            "Director and E-Store Manager to work on it — nothing moves down the pipeline "
            "until the owner approves."
        ),
        "parameters": {"type": "object", "properties": {
            "concept_id": {"type": "integer"},
            "status": {"type": "string",
                       "enum": ["proposed", "approved", "in_production", "live", "retired", "rejected"]},
        }, "required": ["concept_id", "status"]},
    }},
    {"type": "function", "function": {
        "name": "create_product_concept",
        "description": "Record a product idea the owner came up with himself, rather than waiting on the agent.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "product_type": {"type": "string", "enum": ["sticker", "apparel", "metal", "laser", "3d"]},
            "description": {"type": "string"},
            "target_customer": {"type": "string"},
            "price_estimate": {"type": "number"},
            "production_notes": {"type": "string"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "list_art_briefs",
        "description": (
            "Art direction from the Art Director — style direction plus a ready-to-use image "
            "generation prompt for each product."
        ),
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["draft", "approved", "rendered", "rejected"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "set_art_brief_status",
        "description": "Approve, reject, or mark an art brief as rendered once the artwork exists.",
        "parameters": {"type": "object", "properties": {
            "brief_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["draft", "approved", "rendered", "rejected"]},
        }, "required": ["brief_id", "status"]},
    }},
    {"type": "function", "function": {
        "name": "list_store_listings",
        "description": "Store listings the E-Store Manager has drafted, with copy, tags and pricing.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string",
                       "enum": ["draft", "approved", "published", "on_sale", "delisted", "rejected"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "update_store_listing",
        "description": (
            "Approve, edit, price, discount or delist a listing. Note that 'published' records "
            "the owner's decision — it does not itself push anything to a live store, since no "
            "store is connected yet."
        ),
        "parameters": {"type": "object", "properties": {
            "listing_id": {"type": "integer"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "seo_tags": {"type": "string"},
            "price": {"type": "number"},
            "status": {"type": "string",
                       "enum": ["draft", "approved", "published", "on_sale", "delisted", "rejected"]},
        }, "required": ["listing_id"]},
    }},
    {"type": "function", "function": {
        "name": "list_social_posts",
        "description": "Posts the Social Media Director has drafted, with hook, caption and hashtags.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["draft", "approved", "posted", "rejected"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "update_social_post",
        "description": (
            "Approve, edit, schedule or reject a drafted post. Marking one 'posted' records "
            "that the owner published it — nothing is actually sent to any platform, since no "
            "social accounts are connected yet."
        ),
        "parameters": {"type": "object", "properties": {
            "post_id": {"type": "integer"},
            "caption": {"type": "string"},
            "hook": {"type": "string"},
            "hashtags": {"type": "string"},
            "scheduled_for": {"type": "string", "description": "Local ISO 8601 date/time."},
            "status": {"type": "string", "enum": ["draft", "approved", "posted", "rejected"]},
        }, "required": ["post_id"]},
    }},
    {"type": "function", "function": {
        "name": "submit_for_review",
        "description": (
            "Put something in front of the owner on the Review page — the shared surface "
            "where you show him what the team has made. Use it whenever work reaches a point "
            "that needs his call: a finished design, listing copy, a drafted post, a research "
            "brief, or a choice between several variants. Provide 'options' with two or more "
            "entries when he needs to PICK ONE (e.g. three artwork treatments); leave options "
            "out for a straight approve/reject. Attach generated images by passing each "
            "option's file path as media_path so he sees them in the big preview. Tell him "
            "it's waiting for him rather than describing the whole thing in chat."
        ),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Short headline, e.g. 'RVA skyline decal — pick a treatment'."},
            "kind": {"type": "string", "enum": ["concept", "art", "listing", "post", "media", "research", "other"]},
            "summary": {"type": "string", "description": "One or two lines: what this is and what you're asking."},
            "detail": {"type": "string", "description": "Fuller text shown in the preview when there's no image."},
            "source_agent": {"type": "string", "description": "Which agent produced it."},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "ref_table": {"type": "string",
                          "enum": ["product_concepts", "art_briefs", "store_listings", "social_posts"],
                          "description": "Set this when the decision should also advance a real pipeline row."},
            "ref_id": {"type": "integer", "description": "The id in that table."},
            "options": {
                "type": "array",
                "description": "Two or more makes this a pick-one. Omit for approve/reject.",
                "items": {"type": "object", "properties": {
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "media_path": {"type": "string", "description": "Absolute path to a generated image or video."},
                    "body": {"type": "string", "description": "Text content for this option, if it isn't an image."},
                }, "required": ["label"]},
            },
        }, "required": ["title"]},
    }},
    {"type": "function", "function": {
        "name": "list_review_queue",
        "description": (
            "What's stacked up waiting for the owner's decision, and what he's already decided. "
            "Use it for 'what needs my approval', and to check whether he's ruled on something "
            "before you act on it."
        ),
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["pending", "approved", "rejected", "cancelled"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "decide_review_item",
        "description": (
            "Record the owner's decision on a queued item when he tells you in conversation "
            "(e.g. 'approve the second one'). Only ever relay a decision he has actually "
            "made — never decide on his behalf. For a pick-one, pass the option_id he chose."
        ),
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "integer"},
            "decision": {"type": "string", "enum": ["approved", "rejected", "cancelled"]},
            "option_id": {"type": "integer", "description": "Which option he picked, for a choice."},
            "note": {"type": "string", "description": "Anything he said about why."},
        }, "required": ["item_id", "decision"]},
    }},
    {"type": "function", "function": {
        "name": "gpu_status",
        "description": (
            "State of the simrig GPU bridge: whether it's reachable, whether it's currently "
            "reserved for the owner, which models are loaded, VRAM free, and the job queue. "
            "Use this for 'what's the GPU doing', 'is anything queued', or before promising "
            "an agent will get local GPU work done."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "set_gpu_mode",
        "description": (
            "Reserve simrig for the owner, or release it back to the agents. Call this with "
            "'reserved' AS SOON AS he mentions he's gaming, racing, on the sim, streaming, or "
            "otherwise about to use that machine — do not wait to be asked explicitly. It "
            "unloads any resident model and pauses all GPU work; queued jobs simply wait and "
            "resume afterwards. Call it with 'available' when he says he's finished. Mention "
            "briefly what you've done and, if jobs are waiting, how many."
        ),
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["reserved", "available"]},
            "reason": {"type": "string", "description": "e.g. 'racing', 'gaming', 'done racing'."},
        }, "required": ["mode"]},
    }},
    {"type": "function", "function": {
        "name": "gpu_run",
        "description": (
            "Queue a job on the simrig GPU for a local model to handle — image analysis "
            "(task_type 'vision', pass image_path), or cheap text work like tagging, "
            "classifying or summarising ('text'). Use this instead of doing such work "
            "yourself when it's bulk, repetitive, or involves looking at a local image file: "
            "it's free and keeps the subscription for real reasoning. Jobs wait their turn "
            "and wait entirely while simrig is reserved."
        ),
        "parameters": {"type": "object", "properties": {
            "task_type": {"type": "string", "enum": ["vision", "text", "heavy"]},
            "prompt": {"type": "string", "description": "What you want the local model to do."},
            "image_path": {"type": "string", "description": "Absolute path to an image, for 'vision'."},
            "wait": {"type": "boolean", "description": "Wait for the result (default true)."},
        }, "required": ["task_type", "prompt"]},
    }},
    {"type": "function", "function": {
        "name": "generate_media",
        "description": (
            "Generate an actual image or video on simrig. Use this to turn an art brief's "
            "image_prompt into real artwork, or when the owner asks for a picture or a short "
            "clip. Images take under a minute; video takes several minutes and runs in the "
            "background. Files are saved locally and the paths are returned — tell him where "
            "they are. Do not describe what an image looks like until you have actually "
            "generated it and can say the file exists."
        ),
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["image", "video"]},
            "prompt": {"type": "string", "description": "The full image/video generation prompt."},
            "negative": {"type": "string", "description": "What to avoid. Optional."},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "brief_id": {"type": "integer", "description": "Art brief this fulfils, if any."},
            "wait": {"type": "boolean", "description": "Wait for the result. Default true for images, false for video."},
        }, "required": ["kind", "prompt"]},
    }},
    {"type": "function", "function": {
        "name": "add_business_expense",
        "description": "Record money spent on the business (kept separate from personal finances).",
        "parameters": {"type": "object", "properties": {
            "description": {"type": "string"},
            "amount": {"type": "number"},
            "spent_on": {"type": "string", "description": "Date as YYYY-MM-DD. Defaults to today."},
            "category": {"type": "string", "description": "e.g. materials, equipment, fees, software, travel."},
            "notes": {"type": "string"},
        }, "required": ["description", "amount"]},
    }},
    {"type": "function", "function": {
        "name": "list_business_expenses",
        "description": "Business spending, with a total. Use for 'what have I spent on the business'.",
        "parameters": {"type": "object", "properties": {
            "since": {"type": "string", "description": "YYYY-MM-DD. Defaults to the last 90 days."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "add_equipment",
        "description": "Record a piece of business equipment (printer, cutter, press, laser).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "purchase_price": {"type": "number"},
            "purchased_on": {"type": "string", "description": "YYYY-MM-DD."},
            "notes": {"type": "string"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "list_equipment",
        "description": "The business's equipment and its condition.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "set_equipment_status",
        "description": "Update equipment condition — active, needs_service, broken, or sold.",
        "parameters": {"type": "object", "properties": {
            "equipment_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["active", "needs_service", "broken", "sold"]},
        }, "required": ["equipment_id", "status"]},
    }},
    {"type": "function", "function": {
        "name": "update_inventory",
        "description": "Set the stock level of a material (vinyl, blanks, filament, ink).",
        "parameters": {"type": "object", "properties": {
            "item": {"type": "string"},
            "quantity": {"type": "number"},
            "unit": {"type": "string", "description": "e.g. rolls, sheets, kg, each."},
            "reorder_at": {"type": "number", "description": "Warn when stock falls to this."},
            "notes": {"type": "string"},
        }, "required": ["item", "quantity"]},
    }},
    {"type": "function", "function": {
        "name": "list_inventory",
        "description": "Current material stock. Set low_only to see just what needs reordering.",
        "parameters": {"type": "object", "properties": {
            "low_only": {"type": "boolean"},
        }, "required": []},
    }},

    # -- crypto market feed ---------------------------------------------------
    {"type": "function", "function": {
        "name": "market_prices",
        "description": (
            "Current crypto prices from the local LiveCoinWatch cache. Use this instead "
            "of searching the web for a price: it is seconds old, exact, and free. "
            "Omit codes for the top of the market by rank."
        ),
        "parameters": {"type": "object", "properties": {
            "codes": {"type": "array", "items": {"type": "string"},
                      "description": "Ticker symbols, e.g. ['BTC','ETH']."},
            "limit": {"type": "integer", "description": "How many, by rank. Default 25."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "market_movers",
        "description": (
            "Coins that actually moved, biggest absolute change first. This is the right "
            "first call for a monitoring shift -- far better than pulling a price list "
            "and eyeballing it."
        ),
        "parameters": {"type": "object", "properties": {
            "window": {"type": "string", "enum": ["hour", "day", "week", "month"]},
            "min_abs_pct": {"type": "number", "description": "Ignore moves smaller than this. Default 3."},
            "limit": {"type": "integer"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "market_change_since",
        "description": (
            "How far one coin has moved over a window, measured from our own recorded "
            "series rather than a vendor delta field. This is what answers 'what changed "
            "since I last looked' for an employee running every 15 minutes."
        ),
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"},
            "minutes": {"type": "integer", "description": "Look-back window. Default 60."},
        }, "required": ["code"]},
    }},
    {"type": "function", "function": {
        "name": "market_new_listings",
        "description": (
            "Tokens that have entered or left the tracked set recently. A coin appearing "
            "in the top ranks that was not there yesterday is itself a signal, and it is "
            "invisible in any single price response."
        ),
        "parameters": {"type": "object", "properties": {
            "hours": {"type": "integer", "description": "Look-back. Default 24."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "market_feed_status",
        "description": (
            "Health of the price feed: how fresh the data is, how many coins are tracked, "
            "and how much API budget is left. Check this before reporting that nothing is "
            "happening -- a stalled feed and a quiet market look identical otherwise."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},

    # -- paper trading --------------------------------------------------------
    {"type": "function", "function": {
        "name": "paper_portfolio",
        "description": (
            "The simulated crypto portfolio the day trader runs: cash, open positions "
            "marked to live prices, realised and unrealised P&L. No real money is ever "
            "involved and no real order is ever placed. Use this when the owner asks how "
            "the trading is going, what is held, or how a position is doing."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "paper_trade_history",
        "description": (
            "Recent simulated fills, and orders the ledger refused. Refusals matter: an "
            "employee whose orders are all being rejected looks, in the fills alone, "
            "exactly like one that decided not to trade."
        ),
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "How many fills. Default 20."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "paper_performance",
        "description": (
            "Headline performance of the paper account: return since inception, win rate "
            "over closed trades, fees paid, orders rejected."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "paper_reset",
        "description": (
            "Wipe the paper portfolio back to a cash balance, deleting all positions and "
            "trade history. Destructive and irreversible -- only on an explicit request to "
            "reset or restart the simulation, never to tidy up."
        ),
        "parameters": {"type": "object", "properties": {
            "starting_cash": {"type": "number", "description": "New starting cash. Default 10000."},
        }, "required": []},
    }},

    # -- hiring ---------------------------------------------------------------
    {"type": "function", "function": {
        "name": "hire_employee",
        "description": (
            "Hire a new AI employee, configured from a job description. Use when the owner "
            "says he wants to hire someone, add a specialist, or bring on a role — for "
            "example a developer, a front end designer, a copywriter.\n\n"
            "Write the job_description the way a real one is written: years of experience, "
            "specific technologies or disciplines by name, and what the person is "
            "responsible for. That text becomes the employee's actual configuration — "
            "seniority, named skills and voice are all derived from it, so vague input "
            "produces a vague colleague. If the owner gave only a short brief, expand it "
            "into a proper description first, but do not invent skills he did not ask for."
        ),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Job title, e.g. 'Senior Developer'."},
            "job_description": {"type": "string", "description":
                "Full job description: experience, named skills, responsibilities, standards."},
            "cadence": {"type": "string", "enum": ["on_demand", "interval", "daily", "weekly"],
                        "description":
                "How often they work. 'interval' is for monitoring roles that need to "
                "re-check through the day; it requires interval_minutes."},
            "standing_assignment": {"type": "string", "description":
                "Required for any cadence other than on_demand: the work they do each "
                "time it comes round. Without it a scheduled employee is skipped."},
            "interval_minutes": {"type": "integer", "description":
                "For cadence=interval: minutes between runs, e.g. 15. Each run is a real "
                "web search, so below about 10 is wasteful rather than more current."},
            "shift_start": {"type": "string", "description":
                "Local time the shift begins, 'HH:MM'. Omit for around the clock."},
            "shift_end": {"type": "string", "description":
                "Local time the shift ends, 'HH:MM'. May wrap past midnight."},
            "shift_days": {"type": "string", "description":
                "Weekdays they work, comma separated, Monday=0. e.g. '0,1,2,3,4' for "
                "weekdays. Omit for every day."},
            "alert_condition": {"type": "string", "description":
                "Plain-English description of what is worth interrupting the owner for. "
                "Required if alert_policy is not 'never'. Be specific: a vague condition "
                "produces alerts he learns to ignore."},
            "alert_policy": {"type": "string", "enum": ["never", "on_alert", "always"],
                             "description":
                "never = it just files reports; on_alert = notify only when the condition "
                "is met; always = notify every run. Default never."},
            "alert_cooldown_min": {"type": "integer", "description":
                "Minimum minutes between alerts from this employee. Default 30."},
        }, "required": ["title", "job_description"]},
    }},
    {"type": "function", "function": {
        "name": "list_employees",
        "description": "Who currently works here: hired employees, their department, seniority and status.",
        "parameters": {"type": "object", "properties": {
            "include_released": {"type": "boolean"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "assign_work",
        "description": (
            "Give a hired employee a piece of work and get their deliverable back. They can "
            "search the web and produce text or code; they cannot deploy, publish, send or "
            "buy anything. This can take a few minutes."
        ),
        "parameters": {"type": "object", "properties": {
            "employee": {"type": "string", "description": "The employee's key, from list_employees."},
            "assignment": {"type": "string", "description": "What you want them to produce."},
        }, "required": ["employee", "assignment"]},
    }},
    {"type": "function", "function": {
        "name": "manage_employee",
        "description": (
            "Change an employee's standing: pause them, make them active again, release "
            "them, or rewrite their job description (which recompiles how they work)."
        ),
        "parameters": {"type": "object", "properties": {
            "employee": {"type": "string"},
            "action": {"type": "string",
                       "enum": ["pause", "activate", "release", "revise", "set_schedule"]},
            "job_description": {"type": "string", "description": "New description, for 'revise'."},
            "cadence": {"type": "string", "enum": ["on_demand", "interval", "daily", "weekly"]},
            "interval_minutes": {"type": "integer"},
            "shift_start": {"type": "string", "description": "'HH:MM' local"},
            "shift_end": {"type": "string", "description": "'HH:MM' local"},
            "shift_days": {"type": "string", "description": "comma list, Monday=0"},
            "standing_assignment": {"type": "string"},
            "alert_condition": {"type": "string"},
            "alert_policy": {"type": "string", "enum": ["never", "on_alert", "always"]},
            "alert_cooldown_min": {"type": "integer"},
        }, "required": ["employee", "action"]},
    }},
    {"type": "function", "function": {
        "name": "employee_work_history",
        "description": "Recent assignments and deliverables, optionally for one employee.",
        "parameters": {"type": "object", "properties": {
            "employee": {"type": "string"},
            "limit": {"type": "integer"},
        }, "required": []},
    }},
]

BUSINESS_SYSTEM_NOTE = (
    " You are also the owner's second in command for his business, {business_name}, based in "
    "{business_location}. You keep the business's projects, tasks, research, market leads, "
    "product ideas, spending, equipment and materials — treat that as your standing "
    "responsibility, not a filing cabinet you open when asked. When he tells you what he "
    "intends to build, work on, or look into, record it yourself with create_project, "
    "create_task or request_research rather than replying with encouragement and letting it "
    "evaporate; mention briefly that you've noted it. Research is deliberately asynchronous: "
    "request_research queues a background agent that searches the web and writes a real brief, "
    "so say you'll report back rather than pretending to have already read anything. Findings "
    "from the market and trend agents sit in list_market_leads and list_trend_leads for you to "
    "raise when they're relevant.{cadence_note} Business money is separate from his personal "
    "finances: business spending goes to add_business_expense, never to the Era tools, and "
    "questions about business costs come from list_business_expenses, not his bank balance. Be "
    "the one who remembers the state of things and tells him what needs attention."
    " You also run four specialist agents that report to you: a Product Creator (turns trends "
    "into concrete makeable products), an Art Director (style direction and ready-to-use image "
    "generation prompts), an E-Store Manager (listing titles, copy, tags, pricing) and a Social "
    "Media Director (drafts posts per platform). They work as a pipeline and the owner is the "
    "gate between each stage: a concept does nothing until he approves it with set_concept_status, "
    "which is what releases the Art Director and E-Store Manager onto it. Surface their output for "
    "his decision, summarise it in your own words rather than dumping raw fields, and give him a "
    "recommendation — you are his second in command, not a queue he has to empty. Be honest about "
    "one limit: no store or social accounts are connected yet, so 'published' and 'posted' record "
    "his decision and nothing is actually sent anywhere. Never imply a post went live or a product "
    "is on sale somewhere real."
    " You have a shared surface with him: the Review page. When the team finishes something "
    "that needs his call — a design, listing copy, a drafted post, a research brief, or a "
    "choice between variants — put it there with submit_for_review rather than pasting it all "
    "into chat, and say it's waiting for him. Pass two or more options when he needs to pick "
    "one, and attach generated images by their file path so he can actually see them. Check "
    "list_review_queue before acting on anything that needed approval, and never treat an "
    "un-decided item as approved."
    " You can also hire. Beyond the fixed agents above, the owner can add specialists — a "
    "developer, a front end designer, a copywriter — and you create them with hire_employee "
    "from a job description. The job description is not flavour text: it is the "
    "configuration, and seniority, named skills and working voice are all derived from it, "
    "so write a real one (years of experience, specific technologies or disciplines by name, "
    "what they own) rather than a sentence. If he gives only a short brief, expand it into a "
    "proper description first, but never invent skills he did not ask for. list_employees "
    "shows the roster, assign_work gives someone a piece of work, manage_employee pauses, "
    "reactivates, releases or revises a description, and employee_work_history shows what "
    "they have delivered. Hired staff can search the web and produce text or code; they "
    "cannot deploy, publish, send or buy anything, and neither should you claim they did."
    " There is a live crypto price feed: a poller keeps a local LiveCoinWatch cache of "
    "the top 250 coins, refreshed every minute, and market_prices, market_movers, "
    "market_change_since, market_new_listings and market_feed_status read it. Always use "
    "those rather than searching the web for a price -- the cache is seconds old, exact, "
    "and costs nothing, whereas a web search for a price returns something stale dressed "
    "up as current. market_movers is the right first call when asked what is happening. Employees whose work is about crypto or trading are handed that same feed automatically -- current prices, movers over the hour and the day, and listing changes are pasted into their brief on every run, so they are working from live figures even though they hold no tools themselves. If he asks whether an employee can use the market data, the answer is yes and it is already happening; you do not need to grant anything. The day trader also runs a simulated portfolio: it proposes orders and a ledger fills them at cached prices, charging fees, so the results are a real test of its judgement rather than its arithmetic. paper_portfolio, paper_trade_history and paper_performance read it. No real money and no real exchange is involved at any point -- say so plainly if he asks, and never imply a position exists off-system. "
    "Check market_feed_status before reporting that nothing is moving: a stalled feed and "
    "a quiet market are indistinguishable otherwise, and reporting calm when the feed died "
    "is the worst answer available."
    " Employees can work a shift, not just once a day: cadence 'interval' with "
    "interval_minutes runs them repeatedly, and shift_start/shift_end/shift_days bound "
    "that to working hours (leave the hours off for something like crypto that never "
    "closes). Such an employee needs a standing_assignment -- what it re-checks each "
    "time. If it should be able to interrupt him, set alert_policy 'on_alert' and write "
    "a specific alert_condition; it then ends each run with a machine-readable verdict "
    "and only a true verdict reaches his phone, rate-limited by alert_cooldown_min. Push "
    "back on a vague condition or a very short interval: an alert he learns to ignore is "
    "worse than none, and every run is a real web search. Use manage_employee with "
    "action 'set_schedule' to change any of this later without re-hiring."
    "{gpu_note}"
)


GPU_BRIDGE_NOTE = (
    " There is a GPU bridge to simrig, the owner's other machine, running local models for "
    "work that doesn't need you: image analysis and cheap bulk text via gpu_run, queued so only "
    "one job uses the card at a time unless there's VRAM headroom. Prefer it for looking at "
    "local image files and for repetitive tagging or classifying — it's free and keeps the "
    "subscription for real reasoning. It also generates real artwork and video via "
    "generate_media — that is how an Art Director brief stops being a prompt and becomes an "
    "actual file. Images take under a minute; video takes several minutes and runs in the "
    "background, so report it as rendering rather than describing a clip you haven't seen. "
    "Critically, simrig is also the machine he games and "
    "races on: the moment he mentions gaming, racing, sim racing, streaming or otherwise "
    "needing that machine, call set_gpu_mode('reserved') straight away without being asked — "
    "it unloads the models and pauses every GPU job until he says he's done, when you release "
    "it with set_gpu_mode('available'). Queued work simply waits; nothing is lost. Never leave "
    "a model sitting on his card while he's trying to play."
)

AGENTS_SCHEDULED_NOTE = (
    " The agents also run on their own schedule in the background, so new findings can arrive "
    "without anyone asking."
)

AGENTS_ON_DEMAND_NOTE = (
    " Important: every agent is currently set to run ONLY when asked — none of them are on a "
    "schedule, by the owner's deliberate choice while he decides their cadence and working hours "
    "with you. So never say an agent will pick something up later, or that findings will arrive "
    "overnight or in the morning briefing: nothing runs unless you start it with run_business_agent "
    "or he does. When something genuinely warrants a scan, offer to run it now rather than "
    "assuming it's already in hand. If he asks about scheduling them, work out sensible cadence "
    "with him — the setting is business_agents_enabled in config."
)


def _describe_shift(emp) -> str:
    """One human-readable line for when an employee works, for tool results and the UI."""
    cadence = emp["cadence"]
    if cadence == "on_demand":
        return "on demand"
    if cadence == "interval":
        every = f"every {emp['interval_minutes']} min"
    else:
        every = cadence
    window = ""
    if emp["shift_start"] and emp["shift_end"]:
        window = f" between {emp['shift_start']} and {emp['shift_end']}"
    days = ""
    if emp["shift_days"]:
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        try:
            days = " on " + ",".join(names[int(d)] for d in emp["shift_days"].split(","))
        except (ValueError, IndexError):
            days = ""
    return f"{every}{window}{days}"


class BusinessClient:
    """Executes the business tools. Same call_tool shape as the other integrations."""

    def __init__(self, db_path: str, owner_user_id: int, llm=None, profile=None, bridge=None):
        self.db_path = db_path
        self.owner_user_id = owner_user_id
        self.llm = llm
        self.profile = profile
        self.bridge = bridge

    # -- agent triggering ----------------------------------------------------

    def _run_agent_async(self, agent: str) -> dict:
        """Runs a scan off the request thread.

        A market or trend scan makes several web searches and takes minutes — far longer
        than a chat turn should block, and longer than the HTTP request would survive.
        So it's started in the background and reported later, and the tool result says
        exactly that so the model doesn't invent findings it hasn't got.
        """
        if self.llm is None or self.profile is None:
            return {"error": "the background agents are not configured"}
        if not hasattr(self.llm, "research"):
            return {"error": "the current LLM backend cannot search the web, so agents can't run"}

        runners = {
            "market_finder": lambda: agents.run_market_agent(
                self.db_path, self.llm, self.owner_user_id, self.profile),
            "trend_scout": lambda: agents.run_trend_agent(
                self.db_path, self.llm, self.owner_user_id, self.profile),
            "research": lambda: agents.run_research_queue(self.db_path, self.llm, self.profile),
            "product_creator": lambda: agents.run_product_creator(
                self.db_path, self.llm, self.owner_user_id, self.profile),
            "art_director": lambda: agents.run_art_director(
                self.db_path, self.llm, self.owner_user_id, self.profile),
            "store_manager": lambda: agents.run_store_manager(
                self.db_path, self.llm, self.owner_user_id, self.profile),
            "social_director": lambda: agents.run_social_director(
                self.db_path, self.llm, self.owner_user_id, self.profile),
        }
        runner = runners.get(agent)
        if runner is None:
            return {"error": f"unknown agent {agent}"}

        threading.Thread(target=runner, daemon=True, name=f"agent-{agent}").start()
        return {
            "status": "started",
            "agent": agent,
            "message": (
                f"The {agent} scan is now running in the background. It takes several minutes. "
                "Report that it has started — you do NOT have its results yet and must not "
                "describe any findings until they are actually available."
            ),
        }

    # -- dispatch ------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict) -> dict:
        db_path, owner = self.db_path, self.owner_user_id

        if name == "business_status":
            month_start = date.today().replace(day=1).isoformat()
            return {
                "projects": business_db.list_projects(db_path, owner, status="active"),
                "open_tasks": business_db.list_tasks(db_path, owner, status="open"),
                "in_progress_tasks": business_db.list_tasks(db_path, owner, status="doing"),
                "new_market_leads": business_db.list_market_leads(db_path, owner, status="new", limit=8),
                "new_trend_leads": business_db.list_trend_leads(db_path, owner, status="new", limit=8),
                "pending_research": business_db.pending_research(db_path, limit=10),
                "spend_this_month": business_db.expense_total(db_path, owner, since=month_start),
                "low_stock": business_db.list_inventory(db_path, owner, low_only=True),
                "recent_agent_runs": business_db.recent_agent_runs(db_path, limit=5),
            }

        if name == "list_projects":
            return {"projects": business_db.list_projects(db_path, owner, arguments.get("status"))}
        if name == "create_project":
            pid = business_db.create_project(db_path, owner, arguments["name"], arguments.get("goal"))
            return {"ok": True, "project_id": pid}
        if name == "update_project":
            ok = business_db.update_project(
                db_path, owner, arguments["project_id"],
                name=arguments.get("name"), goal=arguments.get("goal"), status=arguments.get("status"))
            return {"ok": ok}

        if name == "list_tasks":
            return {"tasks": business_db.list_tasks(
                db_path, owner, arguments.get("status"), arguments.get("project_id"))}
        if name == "create_task":
            tid = business_db.create_task(
                db_path, owner, arguments["text"], arguments.get("project_id"),
                arguments.get("priority", "normal"), arguments.get("due_at"))
            return {"ok": True, "task_id": tid}
        if name == "update_task":
            ok = business_db.update_task(
                db_path, owner, arguments["task_id"], text=arguments.get("text"),
                status=arguments.get("status"), priority=arguments.get("priority"),
                project_id=arguments.get("project_id"))
            return {"ok": ok}

        if name == "request_research":
            rid = business_db.create_research(
                db_path, owner, arguments["topic"], arguments.get("question"), arguments.get("project_id"))
            return {
                "ok": True, "research_id": rid,
                "message": (
                    "Queued for the background research agent. Tell the owner you've put it in "
                    "hand and will report back — you do not have findings yet."
                ),
            }
        if name == "list_research":
            return {"research": business_db.list_research(db_path, owner, arguments.get("limit", 5))}

        if name == "list_market_leads":
            return {"leads": business_db.list_market_leads(
                db_path, owner, arguments.get("status"), arguments.get("min_score", 0))}
        if name == "set_market_lead_status":
            return {"ok": business_db.set_market_lead_status(
                db_path, owner, arguments["lead_id"], arguments["status"])}

        if name == "list_trend_leads":
            return {"leads": business_db.list_trend_leads(db_path, owner, arguments.get("status"))}
        if name == "set_trend_lead_status":
            return {"ok": business_db.set_trend_lead_status(
                db_path, owner, arguments["lead_id"], arguments["status"])}

        if name == "list_product_concepts":
            return {"concepts": business_db.list_product_concepts(db_path, owner, arguments.get("status"))}
        if name == "set_concept_status":
            return {"ok": business_db.set_concept_status(
                db_path, owner, arguments["concept_id"], arguments["status"])}
        if name == "create_product_concept":
            concept_id, created = business_db.create_product_concept(
                db_path, owner, arguments["name"], arguments.get("product_type"),
                arguments.get("description"), arguments.get("target_customer"),
                arguments.get("price_estimate"), arguments.get("production_notes"), source="owner")
            return {"ok": True, "concept_id": concept_id, "created": created}

        if name == "list_art_briefs":
            return {"briefs": business_db.list_art_briefs(db_path, owner, arguments.get("status"))}
        if name == "set_art_brief_status":
            return {"ok": business_db.set_art_brief_status(
                db_path, owner, arguments["brief_id"], arguments["status"])}

        if name == "list_store_listings":
            return {"listings": business_db.list_store_listings(db_path, owner, arguments.get("status"))}
        if name == "update_store_listing":
            return {"ok": business_db.update_store_listing(
                db_path, owner, arguments["listing_id"], title=arguments.get("title"),
                description=arguments.get("description"), seo_tags=arguments.get("seo_tags"),
                price=arguments.get("price"), status=arguments.get("status"))}

        if name == "list_social_posts":
            return {"posts": business_db.list_social_posts(db_path, owner, arguments.get("status"))}
        if name == "update_social_post":
            return {"ok": business_db.update_social_post(
                db_path, owner, arguments["post_id"], caption=arguments.get("caption"),
                hook=arguments.get("hook"), hashtags=arguments.get("hashtags"),
                scheduled_for=arguments.get("scheduled_for"), status=arguments.get("status"))}

        if name == "submit_for_review":
            item_id = business_db.create_review_item(
                db_path, owner, arguments["title"], arguments.get("kind", "other"),
                summary=arguments.get("summary"), detail=arguments.get("detail"),
                source_agent=arguments.get("source_agent"), ref_table=arguments.get("ref_table"),
                ref_id=arguments.get("ref_id"), priority=arguments.get("priority", "normal"),
                options=arguments.get("options"),
            )
            n = len(arguments.get("options") or [])
            return {
                "ok": True, "item_id": item_id,
                "message": (
                    f"On the Review page now{f' with {n} options to choose from' if n > 1 else ''}. "
                    "Tell him it's waiting for him — do not claim he has approved anything."
                ),
            }
        if name == "list_review_queue":
            return {"items": business_db.list_review_items(
                db_path, owner, status=arguments.get("status", "pending"))}
        if name == "decide_review_item":
            item = business_db.decide_review_item(
                db_path, owner, arguments["item_id"], arguments["decision"],
                option_id=arguments.get("option_id"), note=arguments.get("note"))
            if item is None:
                return {"error": "no pending review item with that id (it may already be decided)"}
            # Same write-through the web page does, so a decision relayed through chat
            # advances the pipeline identically to one clicked on the Review page.
            ref_table, ref_id = item.get("ref_table"), item.get("ref_id")
            target = "approved" if arguments["decision"] == "approved" else "rejected"
            if ref_id:
                try:
                    if ref_table == "product_concepts":
                        business_db.set_concept_status(db_path, owner, ref_id, target)
                    elif ref_table == "art_briefs":
                        business_db.set_art_brief_status(db_path, owner, ref_id, target)
                    elif ref_table == "store_listings":
                        business_db.update_store_listing(db_path, owner, ref_id, status=target)
                    elif ref_table == "social_posts":
                        business_db.update_social_post(db_path, owner, ref_id, status=target)
                except Exception:
                    pass
            return {"ok": True, "item": item}

        if name == "generate_media":
            if self.bridge is None:
                return {"error": "the GPU bridge is not configured"}
            kind = arguments["kind"]
            task_type = "video_generation" if kind == "video" else "image_generation"
            options = {k: arguments[k] for k in ("negative", "width", "height") if arguments.get(k)}
            # Video defaults to not waiting: a few seconds of Wan 2.2 takes minutes, far
            # longer than a chat turn should block.
            wait = arguments.get("wait", kind == "image")
            if not wait:
                job_id = self.bridge.submit("art_director", task_type, arguments["prompt"], options=options)
                return {"status": "queued", "job_id": job_id,
                        "message": ("Queued on simrig. You do NOT have the file yet — say it's "
                                    "rendering and report back when it's done.")}
            job = self.bridge.run_sync(
                "art_director", task_type, arguments["prompt"], options=options,
                timeout=300 if kind == "image" else 1800,
            )
            if job.get("status") == "done":
                files = json.loads(job["result"]).get("files", [])
                if arguments.get("brief_id"):
                    business_db.set_art_brief_status(db_path, owner, arguments["brief_id"], "rendered")
                return {"status": "done", "files": files}
            return {"status": job.get("status"), "error": job.get("error"), "job_id": job.get("id")}

        if name in ("gpu_status", "set_gpu_mode", "gpu_run"):
            if self.bridge is None:
                return {"error": "the GPU bridge is not configured"}
            if name == "gpu_status":
                return self.bridge.status()
            if name == "set_gpu_mode":
                mode = self.bridge.set_mode(arguments["mode"], arguments.get("reason"))
                waiting = len(self.bridge.jobs(status="queued", limit=100))
                return {
                    "ok": True, "mode": mode["mode"], "reason": mode.get("reason"),
                    "queued_jobs": waiting,
                    "message": (
                        f"simrig is reserved — GPU work is paused and {waiting} job(s) will wait."
                        if mode["mode"] == "reserved"
                        else f"simrig is back with the agents; {waiting} queued job(s) will resume."
                    ),
                }
            # gpu_run
            images = None
            if arguments.get("image_path"):
                import base64
                import pathlib

                path = pathlib.Path(arguments["image_path"])
                if not path.is_file():
                    return {"error": f"no such image: {path}"}
                images = [base64.b64encode(path.read_bytes()).decode()]
            if arguments.get("wait", True) is False:
                job_id = self.bridge.submit("jarvis", arguments["task_type"], arguments["prompt"], images)
                return {"status": "queued", "job_id": job_id,
                        "message": "Queued. You do not have the result yet."}
            job = self.bridge.run_sync("jarvis", arguments["task_type"], arguments["prompt"], images)
            return {"status": job.get("status"), "result": job.get("result"), "error": job.get("error")}

        if name == "run_business_agent":
            return self._run_agent_async(arguments["agent"])

        if name == "add_business_expense":
            eid = business_db.add_expense(
                db_path, owner, arguments["description"], float(arguments["amount"]),
                arguments.get("spent_on") or date.today().isoformat(),
                arguments.get("category"), arguments.get("notes"))
            return {"ok": True, "expense_id": eid}
        if name == "list_business_expenses":
            since = arguments.get("since") or (date.today() - timedelta(days=90)).isoformat()
            return {
                "since": since,
                "total": business_db.expense_total(db_path, owner, since=since),
                "expenses": business_db.list_expenses(db_path, owner, since=since),
            }

        if name == "add_equipment":
            eid = business_db.add_equipment(
                db_path, owner, arguments["name"], arguments.get("purchase_price"),
                arguments.get("purchased_on"), arguments.get("notes"))
            return {"ok": True, "equipment_id": eid}
        if name == "list_equipment":
            return {"equipment": business_db.list_equipment(db_path, owner)}
        if name == "set_equipment_status":
            return {"ok": business_db.set_equipment_status(
                db_path, owner, arguments["equipment_id"], arguments["status"])}

        if name == "update_inventory":
            business_db.upsert_inventory(
                db_path, owner, arguments["item"], float(arguments["quantity"]),
                arguments.get("unit"), arguments.get("reorder_at"), arguments.get("notes"))
            return {"ok": True}
        if name == "list_inventory":
            return {"inventory": business_db.list_inventory(
                db_path, owner, low_only=bool(arguments.get("low_only")))}

        # -- crypto market feed ----------------------------------------------
        if name == "market_prices":
            return {"coins": market_data.snapshot(
                db_path, codes=arguments.get("codes"),
                limit=int(arguments.get("limit") or 25))}

        if name == "market_movers":
            return {"movers": market_data.movers(
                db_path, window=arguments.get("window", "hour"),
                min_abs_pct=float(arguments.get("min_abs_pct") or 3.0),
                limit=int(arguments.get("limit") or 15))}

        if name == "market_change_since":
            result = market_data.change_since(
                db_path, arguments.get("code", ""),
                minutes=int(arguments.get("minutes") or 60))
            if result is None:
                return {"error": "no recorded history for that coin in that window"}
            return result

        if name == "market_new_listings":
            return {"listings": market_data.new_listings(
                db_path, hours=int(arguments.get("hours") or 24))}

        if name == "market_feed_status":
            return market_data.feed_status(db_path)

        # -- paper trading ---------------------------------------------------
        if name == "paper_portfolio":
            return paper_trading.portfolio(db_path)

        if name == "paper_trade_history":
            return {"fills": paper_trading.recent_trades(
                        db_path, limit=int(arguments.get("limit") or 20)),
                    "rejected": paper_trading.recent_rejections(db_path)}

        if name == "paper_performance":
            return paper_trading.performance(db_path)

        if name == "paper_reset":
            return paper_trading.reset(
                db_path, starting_cash=float(arguments.get("starting_cash") or 10_000))

        # -- hiring ----------------------------------------------------------
        if name == "hire_employee":
            try:
                emp = staff.hire(
                    db_path,
                    arguments.get("title", ""),
                    arguments.get("job_description", ""),
                    cadence=arguments.get("cadence", "on_demand"),
                    standing_assignment=arguments.get("standing_assignment"),
                    interval_minutes=arguments.get("interval_minutes"),
                    shift_start=arguments.get("shift_start"),
                    shift_end=arguments.get("shift_end"),
                    shift_days=arguments.get("shift_days"),
                    alert_condition=arguments.get("alert_condition"),
                    alert_policy=arguments.get("alert_policy", "never"),
                    alert_cooldown_min=int(arguments.get("alert_cooldown_min") or 30))
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            # Report the derived configuration back, so the owner sees what the job
            # description actually produced rather than having to trust that it landed.
            return {"ok": True, "hired": {
                "key": emp["key"], "title": emp["title"],
                "department": emp["department"], "seniority": emp["seniority"],
                "skills": json.loads(emp["skills"] or "[]"),
                "can": staff.TIER_DESCRIPTIONS.get(emp["capability_tier"], ""),
                "cadence": emp["cadence"],
                "schedule": _describe_shift(emp),
                "alerts": emp["alert_policy"],
            }}

        if name == "list_employees":
            people = staff.list_staff(
                db_path, include_released=bool(arguments.get("include_released")))
            return {"employees": [{
                "key": p["key"], "title": p["title"], "department": p["department"],
                "seniority": p["seniority"], "status": p["status"],
                "skills": json.loads(p["skills"] or "[]"),
                "can": staff.TIER_DESCRIPTIONS.get(p["capability_tier"], ""),
                "schedule": _describe_shift(p),
                "alerts": p["alert_policy"],
            } for p in people]}

        if name == "assign_work":
            if self.llm is None:
                return {"ok": False, "error": "no language model is configured"}
            return staff.assign(db_path, self.llm,
                                arguments.get("employee", ""),
                                arguments.get("assignment", ""))

        if name == "manage_employee":
            key = arguments.get("employee", "")
            action = arguments.get("action", "")
            if action == "revise":
                jd = arguments.get("job_description", "")
                if len(jd.strip()) < 20:
                    return {"ok": False, "error": "the new job description is too thin"}
                emp = staff.revise_job_description(db_path, key, jd)
                if emp is None:
                    return {"ok": False, "error": f"no employee {key!r}"}
                return {"ok": True, "revised": {
                    "key": emp["key"], "department": emp["department"],
                    "seniority": emp["seniority"],
                    "skills": json.loads(emp["skills"] or "[]")}}
            if action == "set_schedule":
                try:
                    emp = staff.set_shift(
                        db_path, key,
                        cadence=arguments.get("cadence"),
                        interval_minutes=arguments.get("interval_minutes"),
                        shift_start=arguments.get("shift_start"),
                        shift_end=arguments.get("shift_end"),
                        shift_days=arguments.get("shift_days"),
                        alert_condition=arguments.get("alert_condition"),
                        alert_policy=arguments.get("alert_policy"),
                        alert_cooldown_min=arguments.get("alert_cooldown_min"))
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                if emp is None:
                    return {"ok": False, "error": f"no employee {key!r}"}
                if arguments.get("standing_assignment"):
                    staff.set_standing_assignment(db_path, key, arguments["standing_assignment"])
                    emp = staff.get_staff(db_path, key)
                return {"ok": True, "schedule": _describe_shift(emp),
                        "alerts": emp["alert_policy"]}

            status = {"pause": "paused", "activate": "active", "release": "released"}.get(action)
            if status is None:
                return {"ok": False, "error": f"unknown action {action!r}"}
            if not staff.set_status(db_path, key, status):
                return {"ok": False, "error": f"no employee {key!r}"}
            return {"ok": True, "employee": key, "status": status}

        if name == "employee_work_history":
            return {"work": staff.recent_work(
                db_path, key=arguments.get("employee") or None,
                limit=int(arguments.get("limit") or 20))}

        return {"error": f"unknown business tool {name}"}
