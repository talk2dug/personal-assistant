"""Background business agents — Jarvis as second in command.

Salvaged in spirit from the Blue Ridge Custom Co project's server/modules (market-finder,
trend-monitor, the sales/marketing "team" standups). That system ran ~40 Node modules on
scraped Eventbrite/Reddit feeds; these are Python rewrites of the three that actually
generalise past a business tied to one city, and they use real web search instead of
brittle scrapers.

Design rules, learned from that project and from this one:

- **Agents gather, they never act.** Each run writes rows (market_leads, trend_leads,
  business_research) for the owner to review, and the ClaudeCLIClient.research() path
  they use has no Jarvis tools at all. An unattended timer job must not be able to send
  an email, spend money, or unlock a door. Anything actionable stays a suggestion until
  the owner says so in chat, where the existing confirmation gate applies.
- **Every run is journalled** to agent_runs, so the digest reports what actually happened
  rather than asking a model to recall it.
- **Nothing is market-specific.** The business profile (city, radius, product lines)
  comes from config, because the whole reason the original was worth rewriting is that
  it hardcoded Asheville and the business moved.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from . import business_db

logger = logging.getLogger(__name__)

# The staff, in one place, so the API and the office UI can't disagree about who works
# here or what they're called. `desk` is the order they're seated in; `character` picks
# which sprite sheet they wear. `department` uses the same vocabulary as hired staff's
# department column (staff.py's CAPABILITY_TIERS) -- engineering/design/creative/
# research/marketing/commerce/operations/general -- so built-in agents and hired
# employees group under one taxonomy in the UI rather than two different label sets.
AGENT_ROSTER = [
    {"key": "market_finder", "title": "Market Finder", "role": "scout", "department": "research", "character": 0},
    {"key": "trend_scout", "title": "Trend Scout", "role": "scout", "department": "research", "character": 1},
    {"key": "research", "title": "Research", "role": "analyst", "department": "research", "character": 2},
    {"key": "product_creator", "title": "Product Creator", "role": "maker", "department": "design", "character": 3},
    {"key": "art_director", "title": "Art Director", "role": "creative", "department": "creative", "character": 4},
    {"key": "store_manager", "title": "E-Store Manager", "role": "commerce", "department": "commerce", "character": 5},
    {"key": "social_director", "title": "Social Media Director", "role": "comms", "department": "marketing", "character": 1},
]

MARKET_SYSTEM = (
    "You research local vendor opportunities for a small maker business. You search the "
    "web for real, currently-listed events and report only what you actually found. "
    "Never invent an event, a date, a venue, or an application fee — a fabricated market "
    "wastes a real trip. If you cannot confirm something, leave that field null."
)

TREND_SYSTEM = (
    "You spot consumer trends that a small custom-print business could turn into "
    "products. You search the web for what is genuinely trending now and report only "
    "real findings, never invented ones."
)

RESEARCH_SYSTEM = (
    "You are Jarvis, researching on behalf of the owner of a small custom-print business. "
    "Search the web, be concrete and practical, and cite where things came from. If the "
    "answer is genuinely uncertain, say so rather than presenting a guess as fact."
)


def _extract_json(text: str):
    """Models wrap JSON in prose or fences no matter how firmly you ask them not to."""
    if not text:
        return None
    cleaned = text.strip()
    if "```" in cleaned:
        blocks = cleaned.split("```")
        for block in blocks:
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block.startswith("[") or block.startswith("{"):
                cleaned = block
                break
    start = min([i for i in (cleaned.find("["), cleaned.find("{")) if i != -1], default=-1)
    if start == -1:
        return None
    end = max(cleaned.rfind("]"), cleaned.rfind("}"))
    if end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None


def _profile_text(profile) -> str:
    lines = [f"Business: {profile.name}", f"Based in: {profile.location}"]
    if profile.product_lines:
        lines.append("Products: " + "; ".join(profile.product_lines))
    if profile.notes:
        lines.append("Context: " + profile.notes)
    return "\n".join(lines)


def run_market_agent(db_path: str, llm, owner_user_id: int, profile) -> dict:
    """Finds vendor markets, craft fairs and pop-ups near the business, scored for fit."""
    run_id = business_db.start_agent_run(db_path, "market_finder")
    try:
        prompt = (
            f"{_profile_text(profile)}\n\n"
            f"Search the web for upcoming craft fairs, maker markets, vendor events, pop-up "
            f"markets and artisan shows within {profile.radius_miles} miles of "
            f"{profile.location}, happening in the next 4 months. Prioritise ones still "
            f"accepting vendor applications.\n\n"
            f"For each event, score fit 0-100 for this business's products. Score low for "
            f"events that would not accept or suit them: food-only, produce/farmers markets, "
            f"juried fine-art-only shows, MLM/direct-sales events, charity-only booths.\n\n"
            f"Return ONLY a JSON array, no prose, of at most 12 objects with keys: "
            f'"name", "event_date" (YYYY-MM-DD or null), "location", "url", "cost" '
            f'(vendor/booth fee as text, or null), "fit_score" (integer), "reasoning" '
            f"(one sentence). Use null for anything you could not verify."
        )
        raw = llm.research(prompt, system_prompt=MARKET_SYSTEM, timeout=600)
        events = _extract_json(raw)
        if not isinstance(events, list):
            business_db.finish_agent_run(
                db_path, run_id, "error", "Could not parse the market scan results.", (raw or "")[:2000])
            return {"status": "error", "new": 0}

        new_count, kept = 0, 0
        for event in events:
            if not isinstance(event, dict) or not event.get("name"):
                continue
            score = event.get("fit_score")
            score = int(score) if isinstance(score, (int, float)) else None
            if score is not None and score < profile.min_fit_score:
                continue
            kept += 1
            if business_db.upsert_market_lead(
                db_path, owner_user_id, str(event["name"]), event.get("event_date"),
                event.get("location"), event.get("url"), event.get("cost"), score,
                event.get("reasoning"),
            ):
                new_count += 1

        summary = f"Market scan: {kept} relevant events, {new_count} new."
        business_db.finish_agent_run(db_path, run_id, "ok", summary, json.dumps(events)[:4000])
        return {"status": "ok", "new": new_count, "kept": kept, "summary": summary}
    except Exception as e:
        logger.exception("market agent failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Market scan failed: {e}")
        return {"status": "error", "new": 0, "error": str(e)}


def run_trend_agent(db_path: str, llm, owner_user_id: int, profile) -> dict:
    """Looks for trends this business could turn into products."""
    run_id = business_db.start_agent_run(db_path, "trend_scout")
    try:
        existing = [t["topic"] for t in business_db.list_trend_leads(db_path, owner_user_id, limit=40)]
        avoid = ("\n\nAlready captured, do not repeat: " + "; ".join(existing[:40])) if existing else ""
        prompt = (
            f"{_profile_text(profile)}\n\n"
            f"Search the web for what is genuinely trending right now — viral moments, "
            f"memes with staying power, local {profile.location} interest, seasonal hooks, "
            f"and rising search interest — that this business could turn into a sticker, "
            f"shirt, decal, metal print or 3D-printed product in the next few weeks.\n\n"
            f"Skip anything offensive, tragedy-related, or legally risky (no copyrighted "
            f"characters, team logos, or brand marks).{avoid}\n\n"
            f"Return ONLY a JSON array, no prose, of at most 8 objects with keys: "
            f'"topic", "source" (where you saw it), "score" (0-100 commercial potential), '
            f'"product_idea" (one concrete product), "reasoning" (one sentence).'
        )
        raw = llm.research(prompt, system_prompt=TREND_SYSTEM, timeout=600)
        trends = _extract_json(raw)
        if not isinstance(trends, list):
            business_db.finish_agent_run(
                db_path, run_id, "error", "Could not parse the trend scan results.", (raw or "")[:2000])
            return {"status": "error", "new": 0}

        new_count = 0
        for trend in trends:
            if not isinstance(trend, dict) or not trend.get("topic"):
                continue
            score = trend.get("score")
            score = int(score) if isinstance(score, (int, float)) else None
            if business_db.upsert_trend_lead(
                db_path, owner_user_id, str(trend["topic"]), trend.get("source"), score,
                trend.get("product_idea"), trend.get("reasoning"),
            ):
                new_count += 1

        summary = f"Trend scan: {new_count} new ideas."
        business_db.finish_agent_run(db_path, run_id, "ok", summary, json.dumps(trends)[:4000])
        return {"status": "ok", "new": new_count, "summary": summary}
    except Exception as e:
        logger.exception("trend agent failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Trend scan failed: {e}")
        return {"status": "error", "new": 0, "error": str(e)}


def run_research_queue(db_path: str, llm, profile, limit: int = 2) -> dict:
    """Works through research the owner asked for in conversation.

    This is the "relay it to Jarvis and he looks into it" path: request_research puts a
    row in the queue during a chat turn, and this picks it up in the background so a
    genuinely deep question isn't bounded by how long the owner will sit and wait.
    """
    queued = business_db.pending_research(db_path, limit=limit)
    if not queued:
        return {"status": "skipped", "done": 0}

    run_id = business_db.start_agent_run(db_path, "research")
    done = 0
    topics = []
    try:
        for item in queued:
            prompt = (
                f"{_profile_text(profile)}\n\n"
                f"Research topic: {item['topic']}\n"
                f"Specific question: {item.get('question') or 'General practical overview.'}\n\n"
                f"Search the web and write a practical brief for the owner: what matters, "
                f"real numbers/costs where you can find them, concrete recommended next "
                f"steps, and anything that would be a mistake. Be concise — aim for under "
                f"400 words. End with a short 'Sources:' list of the URLs you actually used."
            )
            try:
                findings = llm.research(prompt, system_prompt=RESEARCH_SYSTEM, timeout=900)
                business_db.complete_research(db_path, item["id"], findings or "(no findings returned)")
                topics.append(item["topic"])
                done += 1
            except Exception as e:
                logger.exception("research item %s failed", item["id"])
                business_db.complete_research(db_path, item["id"], f"Research failed: {e}", status="failed")

        summary = f"Research: completed {done} ({', '.join(topics)})." if done else "Research: all items failed."
        business_db.finish_agent_run(db_path, run_id, "ok" if done else "error", summary)
        return {"status": "ok", "done": done, "summary": summary}
    except Exception as e:
        logger.exception("research queue failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Research queue failed: {e}")
        return {"status": "error", "done": done}


# =============================================================================
# The creative pipeline: Product Creator -> Art Director -> E-Store -> Social
# =============================================================================
#
# These four hold job titles that imply acting on the world — posting, listing,
# publishing. They deliberately do not. Each produces reviewable work (a concept, an
# art brief, listing copy, a draft post) that the owner approves in chat before it
# moves downstream, and nothing here has a credential to publish with. That is partly
# safety and partly honesty: an agent that posts to a real brand account unattended can
# do damage no confirmation gate can undo. When accounts are connected, the publishing
# step slots in at the approved -> published transition, not inside these functions.

PRODUCT_CREATOR_SYSTEM = (
    "You are a product developer for a small custom-print business. You turn trends and "
    "market signals into specific, makeable products — never vague categories. You know "
    "the constraints of vinyl cutting, DTF apparel printing, metal sublimation, laser "
    "engraving and FDM 3D printing, and you only propose things this shop can actually "
    "produce. You avoid anything trading on copyrighted characters, team logos or brand "
    "marks, because the shop cannot sell those."
)

ART_DIRECTOR_SYSTEM = (
    "You are an art director for a custom-print business. You write image-generation "
    "prompts and design direction with a strong, current visual point of view. Prompts "
    "must be specific about composition, lighting, palette, and finish, and must suit the "
    "physical medium — a vinyl decal needs bold flat shapes and clean cut lines, a metal "
    "print needs dramatic lighting on a dark ground, apparel needs a design that reads at "
    "arm's length on fabric."
)

STORE_MANAGER_SYSTEM = (
    "You are an e-commerce manager for a small custom-print business. You write listing "
    "copy that sells without hype: a clear benefit-led title, a description that answers "
    "what it is, what it's made of, and who it's for, and honest SEO tags people actually "
    "search. You never invent specifications, dimensions, or materials you weren't told."
)

SOCIAL_DIRECTOR_SYSTEM = (
    "You are a social media director for a small maker business. You write posts in the "
    "owner's voice — human, specific, not corporate. You know each platform's texture and "
    "you never write the same caption twice. You never invent reviews, sales figures, or "
    "customer quotes."
)

# Salvaged from the predecessor project's leonardo-prompt-generator.js, whose per-medium
# templates were the genuinely hard-won part: what a prompt needs to say to produce an
# image that survives being printed on that surface.
MEDIUM_GUIDANCE = {
    "metal": (
        "Metal sublimation print: isolated subject on a pure black background, dramatic "
        "studio lighting, sharp specular reflections, photorealistic, ultra detailed."
    ),
    "apparel": (
        "DTF apparel print: bold graphic that reads at arm's length, limited palette, "
        "clean edges, no fine gradients or thin hairlines, transparent background, "
        "centred composition."
    ),
    "sticker": (
        "Die-cut vinyl sticker: bold flat shapes, strong outline suitable for a contour "
        "cut path, high contrast, no thin details that would tear, transparent background."
    ),
    "laser": (
        "Laser engraving: single-colour line art or solid fills only, no gradients or "
        "photographic shading, strong silhouette, high contrast against the substrate."
    ),
    "3d": (
        "Product render of a 3D printed item: clean studio lighting, neutral background, "
        "visible layer texture kept subtle, shown in realistic use."
    ),
}

NEGATIVE_PROMPT = (
    "blurry, low quality, distorted, deformed, bad anatomy, extra limbs, cropped, "
    "watermark, signature, text artifacts, jpeg artifacts, cluttered, muddy colours"
)


def _medium_guidance(product_type: str | None) -> str:
    key = (product_type or "").lower()
    for name, guidance in MEDIUM_GUIDANCE.items():
        if name in key:
            return guidance
    return MEDIUM_GUIDANCE["sticker"]


def run_product_creator(db_path: str, llm, owner_user_id: int, profile, limit: int = 4) -> dict:
    """Turns the best unworked trend leads into concrete, makeable product concepts."""
    run_id = business_db.start_agent_run(db_path, "product_creator")
    try:
        leads = business_db.list_trend_leads(db_path, owner_user_id, status="new", limit=limit)
        if not leads:
            business_db.finish_agent_run(db_path, run_id, "skipped", "No new trend leads to work from.")
            return {"status": "skipped", "new": 0}

        lead_text = "\n".join(
            f"- {lead['topic']} (score {lead['score']}): {lead.get('product_idea') or ''}" for lead in leads)
        existing = [c["name"] for c in business_db.list_product_concepts(db_path, owner_user_id, limit=40)]
        avoid = ("\n\nAlready proposed, do not repeat: " + "; ".join(existing[:40])) if existing else ""

        prompt = (
            f"{_profile_text(profile)}\n\n"
            f"Trend signals to work from:\n{lead_text}{avoid}\n\n"
            f"Propose up to {limit} specific products this shop could actually make and sell. "
            f"Each must be one concrete item, not a category — 'RVA skyline die-cut vinyl "
            f"decal, 4in, matte white' not 'local pride stickers'.\n\n"
            f"Return ONLY a JSON array, no prose, of objects with keys: \"name\", "
            f'"product_type" (one of: sticker, apparel, metal, laser, 3d), "description", '
            f'"target_customer", "price_estimate" (number, USD retail), "production_notes" '
            f"(materials, size, and the steps to make it on the equipment listed above), "
            f'"trend_topic" (which signal above it came from).'
        )
        raw = llm.research(prompt, system_prompt=PRODUCT_CREATOR_SYSTEM, timeout=600)
        concepts = _extract_json(raw)
        if not isinstance(concepts, list):
            business_db.finish_agent_run(
                db_path, run_id, "error", "Could not parse product concepts.", (raw or "")[:2000])
            return {"status": "error", "new": 0}

        lead_by_topic = {lead["topic"]: lead["id"] for lead in leads}
        new_count = 0
        for concept in concepts:
            if not isinstance(concept, dict) or not concept.get("name"):
                continue
            price = concept.get("price_estimate")
            _, created = business_db.create_product_concept(
                db_path, owner_user_id, str(concept["name"]), concept.get("product_type"),
                concept.get("description"), concept.get("target_customer"),
                float(price) if isinstance(price, (int, float)) else None,
                concept.get("production_notes"), source="trend",
                trend_lead_id=lead_by_topic.get(concept.get("trend_topic")),
            )
            if created:
                new_count += 1

        summary = f"Product creator: {new_count} new concepts proposed."
        business_db.finish_agent_run(db_path, run_id, "ok", summary, json.dumps(concepts)[:4000])
        return {"status": "ok", "new": new_count, "summary": summary}
    except Exception as e:
        logger.exception("product creator failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Product creator failed: {e}")
        return {"status": "error", "new": 0}


def run_art_director(db_path: str, llm, owner_user_id: int, profile, limit: int = 3) -> dict:
    """Gives approved concepts artwork direction and a ready-to-use image prompt."""
    run_id = business_db.start_agent_run(db_path, "art_director")
    try:
        concepts = business_db.concepts_without(db_path, owner_user_id, "art_briefs", limit=limit)
        if not concepts:
            business_db.finish_agent_run(
                db_path, run_id, "skipped", "No approved concepts waiting on artwork.")
            return {"status": "skipped", "new": 0}

        new_count = 0
        for concept in concepts:
            prompt = (
                f"{_profile_text(profile)}\n\n"
                f"Product: {concept['name']}\n"
                f"Type: {concept.get('product_type')}\n"
                f"Description: {concept.get('description')}\n"
                f"Audience: {concept.get('target_customer')}\n\n"
                f"Medium requirements: {_medium_guidance(concept.get('product_type'))}\n\n"
                f"Search the web if you need to check what the current visual treatment of "
                f"this subject looks like, then art-direct it.\n\n"
                f"Return ONLY a JSON object, no prose, with keys: \"style_direction\" "
                f"(2-3 sentences on the visual approach and why it suits this audience and "
                f'medium), "image_prompt" (a single complete prompt ready to paste into an '
                f'image generator, incorporating the medium requirements), "aspect" '
                f'(e.g. "1:1", "4:5", "3:2"), "notes" (anything the maker needs to know — '
                f"colour count, bleed, minimum stroke width, cut-line considerations)."
            )
            raw = llm.research(prompt, system_prompt=ART_DIRECTOR_SYSTEM, timeout=600)
            brief = _extract_json(raw)
            if not isinstance(brief, dict) or not brief.get("image_prompt"):
                continue
            business_db.create_art_brief(
                db_path, owner_user_id, title=concept["name"], concept_id=concept["id"],
                style_direction=brief.get("style_direction"), image_prompt=brief.get("image_prompt"),
                negative_prompt=NEGATIVE_PROMPT, aspect=brief.get("aspect"), notes=brief.get("notes"),
            )
            new_count += 1

        summary = f"Art director: {new_count} briefs written."
        business_db.finish_agent_run(db_path, run_id, "ok" if new_count else "error", summary)
        return {"status": "ok" if new_count else "error", "new": new_count, "summary": summary}
    except Exception as e:
        logger.exception("art director failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Art director failed: {e}")
        return {"status": "error", "new": 0}


def run_store_manager(db_path: str, llm, owner_user_id: int, profile, limit: int = 3) -> dict:
    """Writes listing copy for approved concepts, and flags listings that aren't selling."""
    run_id = business_db.start_agent_run(db_path, "store_manager")
    try:
        concepts = business_db.concepts_without(db_path, owner_user_id, "store_listings", limit=limit)
        new_count = 0
        for concept in concepts:
            prompt = (
                f"{_profile_text(profile)}\n\n"
                f"Write the store listing for this product.\n"
                f"Name: {concept['name']}\n"
                f"Type: {concept.get('product_type')}\n"
                f"Description: {concept.get('description')}\n"
                f"Audience: {concept.get('target_customer')}\n"
                f"Estimated retail: {concept.get('price_estimate')}\n"
                f"Production notes: {concept.get('production_notes')}\n\n"
                f"Do not invent dimensions, materials or specifications beyond what's above.\n\n"
                f"Return ONLY a JSON object, no prose, with keys: \"title\" (under 70 chars, "
                f'benefit-led, searchable), "description" (2-4 short paragraphs, plain '
                f'language, no hype), "seo_tags" (comma-separated, terms people actually '
                f'search), "price" (number, USD), "variants" (comma-separated sizes/colours '
                f"or an empty string if there are none)."
            )
            raw = llm.research(prompt, system_prompt=STORE_MANAGER_SYSTEM, timeout=600)
            listing = _extract_json(raw)
            if not isinstance(listing, dict) or not listing.get("title"):
                continue
            price = listing.get("price")
            business_db.create_store_listing(
                db_path, owner_user_id, title=str(listing["title"]), concept_id=concept["id"],
                description=listing.get("description"), seo_tags=listing.get("seo_tags"),
                price=float(price) if isinstance(price, (int, float)) else None,
                variants=listing.get("variants"),
            )
            new_count += 1

        if not concepts:
            business_db.finish_agent_run(
                db_path, run_id, "skipped", "No approved concepts waiting on a listing.")
            return {"status": "skipped", "new": 0}

        summary = f"Store manager: {new_count} listings drafted."
        business_db.finish_agent_run(db_path, run_id, "ok" if new_count else "error", summary)
        return {"status": "ok" if new_count else "error", "new": new_count, "summary": summary}
    except Exception as e:
        logger.exception("store manager failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Store manager failed: {e}")
        return {"status": "error", "new": 0}


def run_social_director(db_path: str, llm, owner_user_id: int, profile, limit: int = 3) -> dict:
    """Drafts launch posts for listings that don't have any yet."""
    run_id = business_db.start_agent_run(db_path, "social_director")
    try:
        listings = business_db.listings_without_posts(db_path, owner_user_id, limit=limit)
        if not listings:
            business_db.finish_agent_run(db_path, run_id, "skipped", "No listings waiting on posts.")
            return {"status": "skipped", "new": 0}

        new_count = 0
        for listing in listings:
            prompt = (
                f"{_profile_text(profile)}\n\n"
                f"New product to announce:\n"
                f"Title: {listing['title']}\n"
                f"Description: {listing.get('description')}\n"
                f"Price: {listing.get('price')}\n\n"
                f"Write launch posts for Instagram, Facebook and TikTok. Each should have a "
                f"different angle — do not rewrite one caption three ways. Write in the "
                f"owner's voice: a real person who makes these by hand in "
                f"{profile.location}. No invented reviews, sales numbers or customer quotes.\n\n"
                f"Return ONLY a JSON array, no prose, of objects with keys: \"platform\" "
                f'(instagram, facebook or tiktok), "hook" (the first line that stops the '
                f'scroll), "caption" (the full post body), "hashtags" (space-separated, '
                f'realistic in number for that platform), "call_to_action".'
            )
            raw = llm.research(prompt, system_prompt=SOCIAL_DIRECTOR_SYSTEM, timeout=600)
            posts = _extract_json(raw)
            if not isinstance(posts, list):
                continue
            for post in posts:
                if not isinstance(post, dict) or not post.get("caption"):
                    continue
                business_db.create_social_post(
                    db_path, owner_user_id, platform=str(post.get("platform") or "instagram"),
                    hook=post.get("hook"), caption=post.get("caption"), hashtags=post.get("hashtags"),
                    call_to_action=post.get("call_to_action"),
                    reason=f"Launch post for '{listing['title']}'",
                    listing_id=listing["id"], concept_id=listing.get("concept_id"),
                )
                new_count += 1

        summary = f"Social director: {new_count} posts drafted."
        business_db.finish_agent_run(db_path, run_id, "ok" if new_count else "error", summary)
        return {"status": "ok" if new_count else "error", "new": new_count, "summary": summary}
    except Exception as e:
        logger.exception("social director failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Social director failed: {e}")
        return {"status": "error", "new": 0}


def build_digest(db_path: str, owner_user_id: int, hours: int = 24) -> str | None:
    """Plain-text summary of what the agents turned up, for the Telegram push.

    Returns None when there is genuinely nothing to report — a daily digest that says
    "nothing happened" every day is how people learn to ignore notifications.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    runs = business_db.recent_agent_runs(db_path, since=since, limit=30)
    markets = [m for m in business_db.list_market_leads(db_path, owner_user_id, status="new", limit=6)
               if m["found_at"] >= since]
    trends = [t for t in business_db.list_trend_leads(db_path, owner_user_id, status="new", limit=6)
              if t["found_at"] >= since]
    research = [r for r in business_db.list_research(db_path, owner_user_id, limit=6)
                if r["status"] == "done" and (r.get("completed_at") or "") >= since]
    open_tasks = business_db.list_tasks(db_path, owner_user_id, status="open")
    low_stock = business_db.list_inventory(db_path, owner_user_id, low_only=True)
    # The pipeline stages waiting on the owner — these are the decisions only he can make,
    # so they belong in the briefing even when nothing new arrived overnight.
    concepts = business_db.list_product_concepts(db_path, owner_user_id, status="proposed", limit=6)
    briefs = business_db.list_art_briefs(db_path, owner_user_id, status="draft", limit=6)
    listings = business_db.list_store_listings(db_path, owner_user_id, status="draft", limit=6)
    posts = business_db.list_social_posts(db_path, owner_user_id, status="draft", limit=10)

    if not (markets or trends or research or low_stock or concepts or briefs or listings or posts):
        failures = [r for r in runs if r["status"] == "error"]
        if not failures:
            return None

    lines = ["Business briefing, sir."]

    if markets:
        lines.append("\nNew markets worth a look:")
        for m in markets:
            when = f" — {m['event_date']}" if m.get("event_date") else ""
            cost = f" ({m['cost']})" if m.get("cost") else ""
            lines.append(f"  [{m['fit_score']}] {m['name']}{when}{cost}")
            if m.get("reasoning"):
                lines.append(f"      {m['reasoning']}")

    if trends:
        lines.append("\nProduct ideas from the trend scan:")
        for t in trends:
            lines.append(f"  [{t['score']}] {t['topic']} — {t.get('product_idea') or ''}")

    if research:
        lines.append("\nResearch finished:")
        for r in research:
            lines.append(f"  {r['topic']} (ask me for the detail)")

    if concepts:
        lines.append("\nProduct concepts waiting on your call:")
        for c in concepts:
            price = f" ~${c['price_estimate']:.0f}" if c.get("price_estimate") else ""
            lines.append(f"  #{c['id']} {c['name']} ({c.get('product_type') or '?'}{price})")

    if briefs:
        lines.append("\nArt briefs ready to review:")
        for b in briefs:
            lines.append(f"  #{b['id']} {b['title']}")

    if listings:
        lines.append("\nlistings drafted:".capitalize())
        for listing in listings:
            price = f" — ${listing['price']:.2f}" if listing.get("price") else ""
            lines.append(f"  #{listing['id']} {listing['title']}{price}")

    if posts:
        by_platform = {}
        for p in posts:
            by_platform[p["platform"]] = by_platform.get(p["platform"], 0) + 1
        spread = ", ".join(f"{n} {platform}" for platform, n in by_platform.items())
        lines.append(f"\nSocial posts drafted and awaiting approval: {len(posts)} ({spread})")

    if low_stock:
        lines.append("\nRunning low:")
        for item in low_stock:
            lines.append(f"  {item['item']}: {item['quantity']}{' ' + item['unit'] if item.get('unit') else ''}")

    if open_tasks:
        high = [t for t in open_tasks if t["priority"] == "high"]
        lines.append(f"\nOpen tasks: {len(open_tasks)}" + (f" ({len(high)} high priority)" if high else ""))

    failures = [r for r in runs if r["status"] == "error"]
    if failures:
        lines.append("\nAgent problems:")
        for f in failures[:3]:
            lines.append(f"  {f['agent']}: {f['summary']}")

    return "\n".join(lines)
