"""Background worker for the owner's own delegated errands — "find me a doctor," "look into
whether X is worth it." Deliberately separate from agents.py: that module's whole framing
(AGENT_ROSTER, office sprites, a business profile) is about the print business, and has
nothing to do with the owner's personal life. This is the one place personal_research rows
actually get worked, the same way agents.run_research_queue works business_research rows.
"""
import logging

from . import db, personal_db

logger = logging.getLogger(__name__)

RESEARCH_SYSTEM = (
    "You are Jarvis, researching something for your owner personally rather than for his "
    "business. Search the web, be concrete and practical, and cite where things came from. "
    "If the answer is genuinely uncertain or depends on things you can't know (his insurance, "
    "his exact address), say so and give him the concrete next step rather than a guess."
)


def run_personal_research_queue(db_path: str, llm, owner_user_id: int, limit: int = 2) -> dict:
    """Works through personal errands the owner asked Jarvis to look into.

    Picked up in the background so something like "find me a dentist" isn't bounded by how
    long he'll sit and wait in chat for a web search to finish.
    """
    queued = personal_db.pending_research(db_path, limit=limit)
    if not queued:
        return {"status": "skipped", "done": 0}

    home = db.get_place_by_name(db_path, owner_user_id, "home")
    location_line = ""
    if home:
        if home.get("notes"):
            location_line = f"He lives at/near: {home['notes']}.\n"
        else:
            location_line = f"He lives near latitude {home['latitude']}, longitude {home['longitude']}.\n"

    done = 0
    topics = []
    for item in queued:
        prompt = (
            f"{location_line}"
            f"Errand: {item['topic']}\n"
            f"Specific question: {item.get('question') or 'General practical help with this.'}\n\n"
            f"Search the web and write a practical, concrete answer: real options with names/"
            f"numbers where you can find them (e.g. actual providers, prices, phone numbers, "
            f"hours), and a clear recommended next step. Be concise — aim for under 300 words. "
            f"End with a short 'Sources:' list of the URLs you actually used."
        )
        try:
            findings = llm.research(prompt, system_prompt=RESEARCH_SYSTEM, timeout=900)
            personal_db.complete_research(db_path, item["id"], findings or "(no findings returned)")
            topics.append(item["topic"])
            done += 1
        except Exception as e:
            logger.exception("personal research item %s failed", item["id"])
            personal_db.complete_research(db_path, item["id"], f"Research failed: {e}", status="failed")

    summary = f"Personal research: completed {done} ({', '.join(topics)})." if done else "Personal research: all items failed."
    return {"status": "ok" if done else "error", "done": done, "summary": summary}
