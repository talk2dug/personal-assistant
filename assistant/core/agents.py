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

from . import agent_notes, art_render, business_db, review_examples

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


def run_market_agent(db_path: str, llm, owner_user_id: int, profile, obsidian=None) -> dict:
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
            f"First write REASONING: two to five sentences on what you looked at, what you ruled out and why, and how confident you are. This is read by the owner on his dashboard and by you on your next run, so write it for a person, not as a label. Then give the data as a fenced ```json block. The block is an array of at most 12 objects with keys: "
            f'"name", "event_date" (YYYY-MM-DD or null), "location", "url", "cost" '
            f'(vendor/booth fee as text, or null), "fit_score" (integer), "reasoning" '
            f"(one sentence). Use null for anything you could not verify."
        )
        raw = llm.research(prompt + agent_notes.read_journal(obsidian, "market_finder"), system_prompt=MARKET_SYSTEM, timeout=600)
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
        agent_notes.write_journal(obsidian, "market_finder", summary, reasoning=raw)
        return {"status": "ok", "new": new_count, "kept": kept, "summary": summary}
    except Exception as e:
        logger.exception("market agent failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Market scan failed: {e}")
        return {"status": "error", "new": 0, "error": str(e)}


def run_trend_agent(db_path: str, llm, owner_user_id: int, profile, obsidian=None) -> dict:
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
            f"First write REASONING: two to five sentences on what you looked at, what you ruled out and why, and how confident you are. This is read by the owner on his dashboard and by you on your next run, so write it for a person, not as a label. Then give the data as a fenced ```json block. The block is an array of at most 8 objects with keys: "
            f'"topic", "source" (where you saw it), "score" (0-100 commercial potential), '
            f'"product_idea" (one concrete product), "reasoning" (one sentence).'
        )
        raw = llm.research(prompt + agent_notes.read_journal(obsidian, "trend_scout"), system_prompt=TREND_SYSTEM, timeout=600)
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
        agent_notes.write_journal(obsidian, "trend_scout", summary, reasoning=raw)
        return {"status": "ok", "new": new_count, "summary": summary}
    except Exception as e:
        logger.exception("trend agent failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Trend scan failed: {e}")
        return {"status": "error", "new": 0, "error": str(e)}


def run_research_queue(db_path: str, llm, profile, limit: int = 2, obsidian=None) -> dict:
    """Works through research the owner asked for in conversation.

    This is the "relay it to Jarvis and he looks into it" path: request_research puts a
    row in the queue during a chat turn, and this picks it up in the background so a
    genuinely deep question isn't bounded by how long the owner will sit and wait.

    `obsidian` (an ObsidianClient) additionally files each completed brief into the vault's
    agent folder. Optional, and a failure to write one never fails the run: the findings
    are already committed by complete_research above it. Without a client the queue
    behaves exactly as it did before -- which is the state that left nine completed briefs
    sitting in SQLite while the vault's research folder went weeks without a new note.
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
                # Vault write after the database write, never before: a brief that exists
                # as a note but not as a completed row would be re-researched on the next
                # tick and cost a second full web-search run.
                agent_notes.write_research_brief(
                    obsidian, item["topic"], findings or "",
                    question=item.get("question"), source="business")
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


def _concept_detail(concept: dict) -> str:
    """The parts of a concept he needs in order to judge it, as readable text rather than
    raw JSON -- price and production notes especially, since 'can this shop actually make
    it, at that price' is the whole decision."""
    parts = []
    for label, key in (("Type", "product_type"), ("Audience", "target_customer"),
                       ("Price estimate", "price_estimate"), ("Production", "production_notes"),
                       ("From trend", "trend_topic")):
        value = concept.get(key)
        if value not in (None, ""):
            parts.append(f"{label}: {value}")
    return "\n".join(parts) or None


def run_product_creator(db_path: str, llm, owner_user_id: int, profile, limit: int = 4,
                        obsidian=None) -> dict:
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
        # A list of titles is what it already had, and it is not enough: it knew WHAT it
        # had proposed and never once what he THOUGHT of it. One of his real rejection
        # notes reads "I already have this created. No need to make it again" -- a rule
        # about his workshop no title list could ever convey.
        verdicts = review_examples.build_verdict_briefing(db_path, owner_user_id, "product_creator")

        prompt = (
            f"{_profile_text(profile)}\n\n"
            f"Trend signals to work from:\n{lead_text}{avoid}{verdicts}\n\n"
            f"Propose up to {limit} specific products this shop could actually make and sell. "
            f"Each must be one concrete item, not a category — 'RVA skyline die-cut vinyl "
            f"decal, 4in, matte white' not 'local pride stickers'.\n\n"
            f"First write REASONING: two to five sentences on what you looked at, what you ruled out and why, and how confident you are. This is read by the owner on his dashboard and by you on your next run, so write it for a person, not as a label. Then give the data as a fenced ```json block. The block is an array of objects with keys: \"name\", "
            f'"product_type" (one of: sticker, apparel, metal, laser, 3d), "description", '
            f'"target_customer", "price_estimate" (number, USD retail), "production_notes" '
            f"(materials, size, and the steps to make it on the equipment listed above), "
            f'"trend_topic" (which signal above it came from).'
        )
        raw = llm.research(prompt + agent_notes.read_journal(obsidian, "product_creator"), system_prompt=PRODUCT_CREATOR_SYSTEM, timeout=600)
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
            concept_id, created = business_db.create_product_concept(
                db_path, owner_user_id, str(concept["name"]), concept.get("product_type"),
                concept.get("description"), concept.get("target_customer"),
                float(price) if isinstance(price, (int, float)) else None,
                concept.get("production_notes"), source="trend",
                trend_lead_id=lead_by_topic.get(concept.get("trend_topic")),
            )
            if created:
                new_count += 1
                # Without this the whole creative pipeline deadlocks, silently and
                # indefinitely: a concept lands at status 'proposed', art_director and
                # store_manager both only pick up APPROVED concepts, and nothing ever put
                # the concept in front of him to approve. It ran that way for eight days
                # and produced 60 unreachable concepts while every downstream agent
                # reported "no approved concepts waiting" and looked idle rather than
                # blocked. source_agent is not decoration -- review_examples filters on it
                # to feed his past verdicts back into the next run's prompt.
                business_db.create_review_item(
                    db_path, owner_user_id, str(concept["name"]), kind="concept",
                    summary=concept.get("description"),
                    detail=_concept_detail(concept),
                    source_agent="product_creator",
                    ref_table="product_concepts", ref_id=concept_id,
                )

        # Retire the leads this run worked from, or the feed never advances. Nothing ever
        # wrote this status: list_trend_leads returns the top `limit` by score and the
        # creator handed back the same three every run forever, with 99 others below them
        # never once read. It went unnoticed only because the "already proposed" list in
        # the prompt kept the repeats from landing -- so the real cost was invisible: a
        # web-searching model call every tick, re-reading ideas it had already mined.
        #
        # A lead that produced nothing is 'passed', not left 'new': it was considered, and
        # leaving it would park it at the top of the list to be reconsidered forever.
        made = {c["trend_lead_id"] for c in
                business_db.list_product_concepts(db_path, owner_user_id, limit=limit * 4)
                if c.get("trend_lead_id")}
        for lead in leads:
            business_db.set_trend_lead_status(
                db_path, owner_user_id, lead["id"], "made" if lead["id"] in made else "passed")

        summary = f"Product creator: {new_count} new concepts proposed."
        business_db.finish_agent_run(db_path, run_id, "ok", summary, json.dumps(concepts)[:4000])
        agent_notes.write_journal(obsidian, "product_creator", summary, reasoning=raw)
        return {"status": "ok", "new": new_count, "summary": summary}
    except Exception as e:
        logger.exception("product creator failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Product creator failed: {e}")
        return {"status": "error", "new": 0}


def _directions_from(brief: dict) -> list[dict]:
    """The visual directions in a brief, however the model chose to express them.

    It is asked for a "directions" list, but it sometimes answers in the older single-
    prompt shape. Accepting both costs three lines and turns "the model phrased it
    differently today" from a lost concept into a card with one option instead of three.
    """
    directions = brief.get("directions")
    if isinstance(directions, list) and directions:
        return [d for d in directions if isinstance(d, dict) and d.get("image_prompt")]
    if brief.get("image_prompt"):
        return [{"label": "Direction 1", "rationale": brief.get("style_direction"),
                 "image_prompt": brief["image_prompt"]}]
    return []


def run_art_director(db_path: str, llm, owner_user_id: int, profile, limit: int = 3,
                     bridge=None, obsidian=None) -> dict:
    """Art-directs approved concepts, renders the options, and files them to be picked.

    The render happens before the review card is written, not after it is approved. The
    owner asked for exactly that -- "I need to see the art work and not just text" -- and
    it is also the more honest gate: approving a prompt is approving a guess about what
    the prompt will produce, and half the time the guess is wrong.

    bridge is a gpu_bridge.GpuBridge. Without one the agent still works and still files
    briefs; they are just text, the way they were before.
    """
    run_id = business_db.start_agent_run(db_path, "art_director")
    try:
        concepts = business_db.concepts_without(db_path, owner_user_id, "art_briefs", limit=limit)
        if not concepts:
            business_db.finish_agent_run(
                db_path, run_id, "skipped", "No approved concepts waiting on artwork.")
            return {"status": "skipped", "new": 0}

        # Wait for the card rather than working around it. Every render would block for
        # its full timeout and the run would end up filing exactly the text-only cards
        # this was rewritten to stop filing -- and it would cost a web-searching model
        # call per concept to do it. Doing nothing leaves the concepts unbriefed, so the
        # next tick picks them up as if this run had never happened.
        reserved = art_render.reserved_reason(bridge)
        if reserved:
            note = f"simrig is reserved ({reserved}) — leaving the artwork until it is free."
            business_db.finish_agent_run(db_path, run_id, "skipped", note)
            return {"status": "skipped", "new": 0, "summary": note}

        verdicts = review_examples.build_verdict_briefing(db_path, owner_user_id, "art_director")
        new_count = 0
        rendered_count = 0
        for concept in concepts:
            prompt = (
                f"{_profile_text(profile)}{verdicts}\n\n"
                f"Product: {concept['name']}\n"
                f"Type: {concept.get('product_type')}\n"
                f"Description: {concept.get('description')}\n"
                f"Audience: {concept.get('target_customer')}\n\n"
                f"Medium requirements: {_medium_guidance(concept.get('product_type'))}\n\n"
                f"Search the web if you need to check what the current visual treatment of "
                f"this subject looks like, then art-direct it.\n\n"
                f"Propose {art_render.DEFAULT_DIRECTIONS} GENUINELY DIFFERENT visual "
                f"directions — different composition, palette and treatment, not the same "
                f"idea reworded. Each one will be rendered and the owner picks between the "
                f"actual images, so a direction that only differs in wording wastes his "
                f"time and a render.\n\n"
                f"First write REASONING: two to five sentences on what you looked at, what you ruled out and why, and how confident you are. This is read by the owner on his dashboard and by you on your next run, so write it for a person, not as a label. Then give the data as a fenced ```json block. The block is an object with keys: \"style_direction\" "
                f"(2-3 sentences on the overall visual approach and why it suits this "
                f'audience and medium), "aspect" (e.g. "1:1", "4:5", "3:2"), "notes" '
                f"(anything the maker needs to know — colour count, bleed, minimum stroke "
                f'width, cut-line considerations), and "directions": a list of '
                f'{art_render.DEFAULT_DIRECTIONS} objects each with "label" (2-4 words '
                f'naming the look), "rationale" (one line on what makes this one different '
                f'and who it lands with), and "image_prompt" (a single complete prompt '
                f"ready to paste into an image generator, incorporating the medium "
                f"requirements)."
            )
            raw = llm.research(prompt + agent_notes.read_journal(obsidian, "art_director"), system_prompt=ART_DIRECTOR_SYSTEM, timeout=600)
            brief = _extract_json(raw)
            if not isinstance(brief, dict):
                continue
            directions = _directions_from(brief)
            if not directions:
                continue

            aspect = brief.get("aspect")
            options, failures = art_render.render_directions(
                bridge, directions, aspect=aspect, negative=NEGATIVE_PROMPT)
            if not options:
                continue
            rendered_count += sum(1 for o in options if o.get("media_path"))

            # The brief carries the first direction's prompt as its working one. Which
            # direction actually wins is his call, and approving an option writes that
            # prompt back here (see business_tools.apply_review_decision).
            brief_id = business_db.create_art_brief(
                db_path, owner_user_id, title=concept["name"], concept_id=concept["id"],
                style_direction=brief.get("style_direction"), image_prompt=options[0]["body"],
                negative_prompt=NEGATIVE_PROMPT, aspect=aspect, notes=brief.get("notes"),
                # The picture, not just the words that asked for it. When autopublish is
                # on, file_for_review advances the brief without writing a card, so the
                # options below -- and the images in them -- are dropped. This is the only
                # thing that survives that path, and nothing downstream can print a
                # product from a prompt.
                media_path=options[0].get("media_path"),
            )
            business_db.create_review_item(
                db_path, owner_user_id, f"Art direction: {concept['name']}", kind="art",
                summary=brief.get("style_direction"),
                detail="\n\n".join(p for p in (
                    f"Aspect: {aspect}" if aspect else None,
                    f"Notes: {brief.get('notes')}" if brief.get("notes") else None,
                    # Named plainly rather than hidden: a card showing two pictures where
                    # it says three directions needs to say why, or it reads as a bug.
                    ("Could not render:\n" + "\n".join(failures)) if failures else None,
                ) if p),
                source_agent="art_director", ref_table="art_briefs", ref_id=brief_id,
                options=options,
            )
            new_count += 1

        summary = (f"Art director: {new_count} briefs written, "
                   f"{rendered_count} images rendered to pick from.")
        business_db.finish_agent_run(db_path, run_id, "ok" if new_count else "error", summary)
        agent_notes.write_journal(obsidian, "art_director", summary, reasoning=raw)
        return {"status": "ok" if new_count else "error", "new": new_count,
                "rendered": rendered_count, "summary": summary}
    except Exception as e:
        logger.exception("art director failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Art director failed: {e}")
        return {"status": "error", "new": 0}


def backfill_art_renders(db_path: str, owner_user_id: int, bridge, limit: int = 10) -> dict:
    """Gives the art cards written under the old order their pictures.

    run_art_director now renders before it files, but the cards already in the queue were
    written the other way round and are still sitting there as prose. Rather than raise
    them again -- which would lose their place and their age -- each one keeps its card
    and gains the image its own brief describes.

    One picture each, not three: these briefs were written with a single prompt, and
    inventing two more directions here would be a different decision from the one he was
    asked to make.
    """
    reserved = art_render.reserved_reason(bridge)
    if reserved:
        return {"status": "skipped", "rendered": 0,
                "summary": f"simrig is reserved ({reserved})."}

    pending = [i for i in business_db.list_review_items(db_path, owner_user_id, status="pending")
               if i["kind"] == "art" and i["ref_table"] == "art_briefs" and not i["options"]]
    rendered = 0
    for item in pending[:limit]:
        brief = next((b for b in business_db.list_art_briefs(db_path, owner_user_id, limit=200)
                      if b["id"] == item["ref_id"]), None)
        if brief is None or not brief.get("image_prompt"):
            continue
        options, failures = art_render.render_directions(
            bridge,
            [{"label": "As briefed", "rationale": brief.get("style_direction"),
              "image_prompt": brief["image_prompt"]}],
            aspect=brief.get("aspect"), negative=brief.get("negative_prompt"))
        if not options or not options[0].get("media_path"):
            logger.warning("art backfill could not render brief %s: %s", brief["id"], failures)
            continue
        business_db.add_review_options(db_path, owner_user_id, item["id"], options)
        rendered += 1

    return {"status": "ok", "rendered": rendered, "pending": len(pending),
            "summary": f"Rendered {rendered} of {len(pending)} text-only art cards."}


def run_store_manager(db_path: str, llm, owner_user_id: int, profile, limit: int = 3,
                      obsidian=None) -> dict:
    """Writes listing copy for approved concepts, and flags listings that aren't selling."""
    run_id = business_db.start_agent_run(db_path, "store_manager")
    try:
        concepts = business_db.concepts_without(db_path, owner_user_id, "store_listings", limit=limit)
        verdicts = review_examples.build_verdict_briefing(db_path, owner_user_id, "store_manager")
        new_count = 0
        for concept in concepts:
            prompt = (
                f"{_profile_text(profile)}{verdicts}\n\n"
                f"Write the store listing for this product.\n"
                f"Name: {concept['name']}\n"
                f"Type: {concept.get('product_type')}\n"
                f"Description: {concept.get('description')}\n"
                f"Audience: {concept.get('target_customer')}\n"
                f"Estimated retail: {concept.get('price_estimate')}\n"
                f"Production notes: {concept.get('production_notes')}\n\n"
                f"Do not invent dimensions, materials or specifications beyond what's above.\n\n"
                f"First write REASONING: two to five sentences on what you looked at, what you ruled out and why, and how confident you are. This is read by the owner on his dashboard and by you on your next run, so write it for a person, not as a label. Then give the data as a fenced ```json block. The block is an object with keys: \"title\" (under 70 chars, "
                f'benefit-led, searchable), "description" (2-4 short paragraphs, plain '
                f'language, no hype), "seo_tags" (comma-separated, terms people actually '
                f'search), "price" (number, USD), "variants" (comma-separated sizes/colours '
                f"or an empty string if there are none)."
            )
            raw = llm.research(prompt + agent_notes.read_journal(obsidian, "store_manager"), system_prompt=STORE_MANAGER_SYSTEM, timeout=600)
            listing = _extract_json(raw)
            if not isinstance(listing, dict) or not listing.get("title"):
                continue
            price = listing.get("price")
            listing_id = business_db.create_store_listing(
                db_path, owner_user_id, title=str(listing["title"]), concept_id=concept["id"],
                description=listing.get("description"), seo_tags=listing.get("seo_tags"),
                price=float(price) if isinstance(price, (int, float)) else None,
                variants=listing.get("variants"),
            )
            business_db.create_review_item(
                db_path, owner_user_id, f"Listing: {listing['title']}", kind="listing",
                summary=listing.get("description"),
                detail="\n\n".join(p for p in (
                    f"Price: {price}" if price not in (None, "") else None,
                    f"SEO tags: {listing.get('seo_tags')}" if listing.get("seo_tags") else None,
                    f"Variants: {listing.get('variants')}" if listing.get("variants") else None,
                ) if p),
                source_agent="store_manager", ref_table="store_listings", ref_id=listing_id,
            )
            new_count += 1

        if not concepts:
            business_db.finish_agent_run(
                db_path, run_id, "skipped", "No approved concepts waiting on a listing.")
            return {"status": "skipped", "new": 0}

        summary = f"Store manager: {new_count} listings drafted."
        business_db.finish_agent_run(db_path, run_id, "ok" if new_count else "error", summary)
        agent_notes.write_journal(obsidian, "store_manager", summary, reasoning=raw)
        return {"status": "ok" if new_count else "error", "new": new_count, "summary": summary}
    except Exception as e:
        logger.exception("store manager failed")
        business_db.finish_agent_run(db_path, run_id, "error", f"Store manager failed: {e}")
        return {"status": "error", "new": 0}


def run_social_director(db_path: str, llm, owner_user_id: int, profile, limit: int = 3,
                        obsidian=None) -> dict:
    """Drafts launch posts for listings that don't have any yet."""
    run_id = business_db.start_agent_run(db_path, "social_director")
    try:
        listings = business_db.listings_without_posts(db_path, owner_user_id, limit=limit)
        if not listings:
            business_db.finish_agent_run(db_path, run_id, "skipped", "No listings waiting on posts.")
            return {"status": "skipped", "new": 0}

        verdicts = review_examples.build_verdict_briefing(db_path, owner_user_id, "social_director")
        new_count = 0
        for listing in listings:
            prompt = (
                f"{_profile_text(profile)}{verdicts}\n\n"
                f"New product to announce:\n"
                f"Title: {listing['title']}\n"
                f"Description: {listing.get('description')}\n"
                f"Price: {listing.get('price')}\n\n"
                f"Write launch posts for Instagram, Facebook and TikTok. Each should have a "
                f"different angle — do not rewrite one caption three ways. Write in the "
                f"owner's voice: a real person who makes these by hand in "
                f"{profile.location}. No invented reviews, sales numbers or customer quotes.\n\n"
                f"First write REASONING: two to five sentences on what you looked at, what you ruled out and why, and how confident you are. This is read by the owner on his dashboard and by you on your next run, so write it for a person, not as a label. Then give the data as a fenced ```json block. The block is an array of objects with keys: \"platform\" "
                f'(instagram, facebook or tiktok), "hook" (the first line that stops the '
                f'scroll), "caption" (the full post body), "hashtags" (space-separated, '
                f'realistic in number for that platform), "call_to_action".'
            )
            raw = llm.research(prompt + agent_notes.read_journal(obsidian, "social_director"), system_prompt=SOCIAL_DIRECTOR_SYSTEM, timeout=600)
            posts = _extract_json(raw)
            if not isinstance(posts, list):
                continue
            for post in posts:
                if not isinstance(post, dict) or not post.get("caption"):
                    continue
                platform = str(post.get("platform") or "instagram")
                post_id = business_db.create_social_post(
                    db_path, owner_user_id, platform=platform,
                    hook=post.get("hook"), caption=post.get("caption"), hashtags=post.get("hashtags"),
                    call_to_action=post.get("call_to_action"),
                    reason=f"Launch post for '{listing['title']}'",
                    listing_id=listing["id"], concept_id=listing.get("concept_id"),
                )
                business_db.create_review_item(
                    db_path, owner_user_id, f"{platform.title()} post: {listing['title']}",
                    kind="post", summary=post.get("hook"),
                    detail="\n\n".join(p for p in (
                        post.get("caption"),
                        f"Hashtags: {post.get('hashtags')}" if post.get("hashtags") else None,
                        f"CTA: {post.get('call_to_action')}" if post.get("call_to_action") else None,
                    ) if p),
                    source_agent="social_director", ref_table="social_posts", ref_id=post_id,
                )
                new_count += 1

        summary = f"Social director: {new_count} posts drafted."
        business_db.finish_agent_run(db_path, run_id, "ok" if new_count else "error", summary)
        agent_notes.write_journal(obsidian, "social_director", summary, reasoning=raw)
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
