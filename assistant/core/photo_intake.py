"""What is this a photograph of, and what should be done with it?

Jack sent a recipe to the mail pipeline on purpose, to find out whether anything was
actually reasoning. Nothing was. The pipeline knew one thing -- "read a letter" -- so it
read a cookbook page as a letter, correctly concluded there was no sender and no amount,
filed it in a mail table and said nothing. He got no reply and the recipe was thrown
away. His rule, and it is the right one:

    "If I send a photo Jarvis needs to know what to do with it.
     If it doesn't know then it needs to ask."

That is a different shape from everything the photo code did before. The old flow started
from an assumption about the content and extracted against it. This one LOOKS first,
decides what it is, and only then picks the reader -- and where it cannot decide, it asks
him rather than guessing, because a confident wrong answer costs more than a question.

WHAT IT WILL NOT DO. It does not save anything on a guess. A recipe becomes a draft he
confirms, not a catalogue entry; that is the same call kitchen.py already made, for the
same reason -- reading a cluttered page is lossy and a silently-wrong recipe is worse
than none. The only thing it commits without asking is a task, which is reversible and
is what he asked for in the first place.
"""
import base64
import json
import logging
import os
import re
import shutil
from datetime import datetime

from . import mail_photo

logger = logging.getLogger(__name__)

# What a photograph he sends can turn out to be. Deliberately the set of things Jarvis
# can actually DO something with, plus "other" -- a taxonomy of everything a camera can
# see would just be a longer way of saying "I don't know".
KINDS = ("mail", "recipe", "receipt", "pantry", "artwork", "document", "other")

# How sure it has to be before acting rather than asking. His instruction is explicit:
# when it does not know, it asks. So "medium" is not good enough to file something
# under -- it is good enough to suggest.
ACT_ON = ("high",)

TRIAGE_PROMPT = """Look at this photograph and say what it IS. The person who sent it
wants his assistant to do the right thing with it.

Reply with ONLY a JSON object. Start at the opening brace, explain nothing.

  "kind"        one of:
                  "mail"     - a letter, bill, statement, notice or envelope addressed
                               to someone. Post that came through a door.
                  "recipe"   - a recipe: a cookbook page, a recipe card, a screenshot of
                               ingredients and method.
                  "receipt"  - a till receipt or itemised proof of purchase.
                  "pantry"   - food, groceries, a fridge or cupboard interior.
                  "document" - a form, contract, manual, certificate or printed page that
                               is not post and not a recipe.
                  "other"    - anything else at all.
  "summary"     one plain sentence describing what is actually in the picture
  "title"       a short name for it, five words or fewer, or null
  "confidence"  "high", "medium" or "low" -- how sure you are of "kind"

Be honest about confidence. If it could reasonably be two of these, say low. Getting this
wrong sends the photograph to the wrong place entirely."""

# What Jarvis offers to do, per kind. The wording is what he sees on the Needs You board
# and in the message, so it is written as an answer to "what should I do with this?".
ACTIONS = {
    "mail": "Read it as post and put anything with a deadline on your list",
    "recipe": "Save it to your recipes",
    "receipt": "Log the purchases against your kitchen inventory",
    "pantry": "Update what you have in",
    "artwork": "File it with the artwork",
    "document": "Keep it and note what it is",
}

# What he can write in the subject line to say outright what a photo is. His idea, and
# it outranks anything the model decides: a person telling you what he sent is better
# evidence than a vision model inferring it, and it costs no GPU call at all.
#
# Matched on whole words against the subject, longest phrase first, so "art work" wins
# over "art" and a subject of "artwork for the store" still routes.
SUBJECT_INTENTS = {
    "art work": "artwork", "artwork": "artwork", "art": "artwork",
    "recipe": "recipe", "recipes": "recipe",
    "receipt": "receipt", "receipts": "receipt",
    "mail": "mail", "post": "mail", "bill": "mail", "letter": "mail",
    "pantry": "pantry", "fridge": "pantry", "groceries": "pantry",
    "document": "document", "doc": "document", "form": "document",
}


def triage(bridge, image_bytes: bytes) -> dict:
    """Decide what the photograph is. Never raises; returns kind 'other' on any failure,
    which routes to asking him -- the safe direction."""
    if bridge is None:
        return {"kind": "other", "summary": None, "title": None,
                "confidence": "low", "error": "no vision bridge"}
    try:
        job = bridge.run_sync(
            "photo", "vision", TRIAGE_PROMPT,
            images=[base64.b64encode(image_bytes).decode()],
            options={"num_predict": 512, "num_ctx": 8192}, fmt="json")
    except Exception as exc:                                        # noqa: BLE001
        logger.exception("photo triage: vision call failed")
        return {"kind": "other", "summary": None, "title": None,
                "confidence": "low", "error": str(exc)[:200]}

    if job.get("status") != "done":
        return {"kind": "other", "summary": None, "title": None, "confidence": "low",
                "error": job.get("error") or f"job status: {job.get('status')}"}

    parsed = mail_photo._extract_json(job.get("result") or "")
    if not isinstance(parsed, dict):
        return {"kind": "other", "summary": None, "title": None, "confidence": "low",
                "error": "no JSON in the triage reply"}

    kind = str(parsed.get("kind") or "other").strip().lower()
    confidence = str(parsed.get("confidence") or "low").strip().lower()
    return {
        "kind": kind if kind in KINDS else "other",
        "summary": (str(parsed["summary"]).strip()[:300] if parsed.get("summary") else None),
        "title": (str(parsed["title"]).strip()[:80] if parsed.get("title") else None),
        "confidence": confidence if confidence in ("high", "medium", "low") else "low",
        "error": None,
    }


def intent_from_subject(subject: str | None) -> str | None:
    """What he SAID it is. Beats the model, every time.

    He asked for this directly: *"If I send photos with the subject line art work, it
    should file the image in the media."* An explicit instruction from the person who
    took the photograph is not a hint to weigh against a classifier -- it is the answer.
    """
    if not subject:
        return None
    words = re.findall(r"[a-z]+", subject.lower())
    joined = " ".join(words)
    for phrase in sorted(SUBJECT_INTENTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", joined):
            return SUBJECT_INTENTS[phrase]
    return None


def should_act(verdict: dict) -> bool:
    """Whether Jarvis knows enough to just get on with it.

    'other' never acts even at high confidence -- being certain that something is
    none of the things you can handle is precisely the moment to ask.
    """
    return verdict.get("confidence") in ACT_ON and verdict.get("kind") in ACTIONS


def question_for(verdict: dict) -> str:
    """What Jarvis asks when it does not know. Names what it thinks it is looking at --
    'I don't know what this is' is a worse question than 'is this a recipe?'."""
    seen = verdict.get("summary") or "a photo I can't place"
    if verdict.get("kind") in ACTIONS and verdict.get("confidence") != "high":
        return (f"You sent me {seen[0].lower() + seen[1:]} "
                f"I think it's {verdict['kind']}, but I'm not certain. "
                f"What would you like me to do with it?")
    return f"You sent me {seen[0].lower() + seen[1:]} What should I do with it?"


def options_for(verdict: dict) -> list[dict]:
    """The choices he gets, best guess first so the common case is one tap."""
    ordered = ([verdict["kind"]] if verdict.get("kind") in ACTIONS else []) + \
              [k for k in ACTIONS if k != verdict.get("kind")]
    return [{"label": ACTIONS[k], "value": k} for k in ordered] + \
           [{"label": "Nothing - just keep the photo", "value": "keep"}]


def describe(verdict: dict, outcome: str | None = None) -> str:
    """What Jarvis says back when it DID know what to do. Conversational on purpose:
    'kind=recipe confidence=high' is a log line, not an answer to a person."""
    what = verdict.get("title") or verdict.get("summary") or "that"
    lead = {
        "mail": f"Post: {what}.",
        "recipe": f"That's a recipe - {what}.",
        "receipt": f"Receipt from {what}.",
        "pantry": f"Had a look in - {what}.",
        "document": f"Document: {what}.",
    }.get(verdict.get("kind"), f"{what}.")
    return f"{lead} {outcome}".strip() if outcome else lead


def file_artwork(saved_path: str, media_path: str, subject: str | None) -> str:
    """Put the image with the artwork and say where it went.

    No model call at all. He told us what it is in the subject line, and an image being
    filed as artwork needs nothing read off it -- running a vision pass here would be
    twenty seconds spent confirming something he already said.
    """
    out_dir = os.path.join(media_path, "artwork")
    os.makedirs(out_dir, exist_ok=True)
    # Named from his subject, so the folder is browsable by what he called things
    # rather than by a row of timestamps.
    stem = re.sub(r"[^A-Za-z0-9 _-]", "", (subject or "artwork")).strip() or "artwork"
    stem = re.sub(r"\s+", "-", stem)[:60]
    suffix = os.path.splitext(saved_path)[1] or ".jpg"
    target = os.path.join(out_dir, f"{datetime.now():%Y-%m-%d}-{stem}{suffix}")
    n = 1
    while os.path.exists(target):
        target = os.path.join(out_dir, f"{datetime.now():%Y-%m-%d}-{stem}-{n}{suffix}")
        n += 1
    shutil.copy2(saved_path, target)
    return target


def handle(db_path: str, owner_user_id: int, bridge, image_bytes: bytes,
           saved_path: str, raise_task=None, subject: str | None = None,
           media_path: str = "generated") -> dict:
    """Look at one photograph and do the right thing with it, or ask.

    Returns {"kind", "confidence", "acted", "reply", "review_id"} -- `reply` is what to
    say to him and is never empty, because a photo he sent that produces silence is the
    exact failure this module exists to fix.
    """
    from . import business_db

    # What he WROTE outranks what the model sees. If the subject says "art work", that
    # is not a hint to weigh -- it is the answer, and it saves a GPU call entirely.
    stated = intent_from_subject(subject)
    if stated:
        verdict = {"kind": stated, "confidence": "high", "title": (subject or "").strip(),
                   "summary": None, "error": None}
        logger.info("photo intake: %s, because he said so in the subject %r",
                    stated, subject)
    else:
        verdict = triage(bridge, image_bytes)
        logger.info("photo intake: %s (confidence %s) - %s", verdict["kind"],
                    verdict["confidence"], verdict.get("summary"))

    if verdict["kind"] == "artwork":
        try:
            target = file_artwork(saved_path, media_path, subject)
        except Exception:
            logger.exception("photo intake: could not file the artwork")
            return {"kind": "artwork", "confidence": verdict["confidence"], "acted": False,
                    "reply": "I couldn't file that with the artwork - the photo is kept.",
                    "review_id": None}
        return {"kind": "artwork", "confidence": verdict["confidence"], "acted": True,
                "reply": f"Filed with the artwork: {os.path.basename(target)}",
                "review_id": None}

    if not should_act(verdict):
        # Ask. Filed on the Needs You board with the options, so the question survives a
        # missed text and he can answer it whenever he gets to it.
        review_id = None
        try:
            review_id = business_db.create_review_item(
                db_path, owner_user_id,
                title=verdict.get("title") or "A photo you sent",
                kind="other", source_agent="photo_intake",
                summary=question_for(verdict),
                detail=f"Photo: {saved_path}" + (f"\nYou wrote: {subject}" if subject else ""),
                options=options_for(verdict))
        except Exception:
            logger.exception("photo intake: could not file the question")
        return {"kind": verdict["kind"], "confidence": verdict["confidence"],
                "acted": False, "reply": question_for(verdict), "review_id": review_id}

    if verdict["kind"] == "mail":
        reading = mail_photo.read_photo(bridge, image_bytes)
        piece_id = mail_photo.record_pending(db_path, owner_user_id, saved_path)
        mail_photo.apply_reading(db_path, piece_id, reading)
        if raise_task is not None:
            raise_task(piece_id, reading, saved_path, {"subject": subject})
        return {"kind": "mail", "confidence": verdict["confidence"], "acted": True,
                "reply": _mail_reply(reading), "review_id": None}

    # Everything else becomes a DRAFT he confirms, never a saved record. Reading a
    # cluttered page is lossy, and kitchen.py already made this call for exactly this
    # reason -- a silently-wrong recipe in the catalogue is worse than no recipe.
    draft, outcome = _draft_for(verdict["kind"], bridge, image_bytes)
    review_id = None
    try:
        review_id = business_db.create_review_item(
            db_path, owner_user_id,
            title=verdict.get("title") or verdict["kind"].title(),
            kind="other", source_agent="photo_intake",
            summary=describe(verdict, outcome),
            detail=(json.dumps(draft, indent=2)[:4000] if draft else None)
                   or f"Photo: {saved_path}",
            options=[{"label": "Save it", "value": "save"},
                     {"label": "No, discard", "value": "discard"}])
    except Exception:
        logger.exception("photo intake: could not file the draft")
    return {"kind": verdict["kind"], "confidence": verdict["confidence"], "acted": True,
            "reply": describe(verdict, outcome), "review_id": review_id}


def _draft_for(kind: str, bridge, image_bytes: bytes):
    """Read the photo with the right specialist. Returns (draft, what to tell him)."""
    from . import kitchen_vision

    try:
        if kind == "recipe":
            draft = kitchen_vision.analyze_recipe_photo(bridge, image_bytes)
            if draft.get("parsed"):
                n = len(draft.get("ingredients") or [])
                return draft, f"I've drafted it{f' ({n} ingredients)' if n else ''} - say the word and I'll save it."
            return draft, "I couldn't read it cleanly though - worth a second photo."
        if kind == "receipt":
            draft = kitchen_vision.analyze_receipt_photo(bridge, image_bytes)
            if draft.get("parsed"):
                n = len(draft.get("items") or [])
                return draft, f"{n} item(s) on it - want them logged against the kitchen?"
            return draft, "I couldn't read the lines though - worth a second photo."
        if kind == "pantry":
            draft = kitchen_vision.analyze_inventory_photo(bridge, image_bytes)
            if draft.get("parsed"):
                n = len(draft.get("items") or [])
                return draft, f"I can see {n} item(s) - want me to update what you have in?"
            return draft, "I couldn't make out what's in there - worth a second photo."
    except Exception:
        logger.exception("photo intake: the %s reader failed", kind)
        return None, "I couldn't read it, but I've kept the photo."
    return None, "I've kept it."


def _mail_reply(reading: dict) -> str:
    if not reading.get("parsed"):
        return ("Post, but I couldn't read it - I've kept the photo and put it on your "
                "list to open yourself.")
    bits = []
    sender, summary = reading.get("sender"), reading.get("summary")
    bits.append(f"{sender}: {summary}" if sender and summary else (summary or sender or "Read it."))
    if reading.get("amount") is not None:
        line = f"${reading['amount']:,.2f}"
        if reading.get("due_date"):
            line += f", due {reading['due_date']}"
        bits.append(line)
    bits.append("On your list." if mail_photo.should_raise_task(reading)
                else "Nothing to do.")
    if reading.get("confidence") != "high":
        bits.append("Read off a photo though - worth checking the letter.")
    return " ".join(bits)
