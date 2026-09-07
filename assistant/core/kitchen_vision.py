"""Turns a photo into structured text via the GPU bridge's vision task (qwen3-vl on
simrig) -- the recipe-photo and (later) inventory-photo/receipt-photo features all share
this one call shape: a JPEG in, a JSON object out, forgivingly parsed.

Deliberately not the chat-attached camera snapshot path (JarvisContext.jsx's
toggleCamera/captureFrame, engine.py's inline image_bytes) -- that's ephemeral, tied to
whichever model is running the live conversation, and never persisted. This is a
one-shot, queued, always-qwen3-vl call for a browser file upload, with a real job record.
"""
import base64
import json

# Deliberately includes a worked example for the one case that actually made the model
# loop in testing: "1 large egg", where it isn't obvious whether "large" is a unit or
# part of the ingredient name. Spelling out the answer up front heads off exactly the
# kind of paragraphs-long self-argument that burned through the entire output budget
# without ever reaching JSON in the first real test of this prompt (a 14,000-character
# internal monologue re-litigating this one line, never emitting an answer).
RECIPE_PROMPT = """You are looking at a photo of a recipe -- a handwritten card, a \
cookbook page, a printed recipe, or a screenshot.

Respond with ONLY a single JSON object and nothing else: no preamble, no explanation, \
no re-reading the rules out loud, no markdown code fence. Do not second-guess or revise \
your answer -- pick a reasonable reading on the first pass and output it. Start your \
reply with the opening brace.

Shape:
{"title": "...", "servings": <integer or null>, \
"ingredients": [{"name": "...", "quantity": "...", "unit": "..."}], \
"steps": ["...", "..."]}

Rules:
- One ingredient entry per line item, in the order listed. quantity and unit are always \
strings, and may be "" if the photo doesn't give one. Descriptive words like "large", \
"chopped", or "fresh" belong in unit or name, your choice -- do not deliberate over it. \
Example: "1 large egg" -> {"name": "egg", "quantity": "1", "unit": "large"}.
- steps: the ordered instructions, one string per step. Keep wording close to what's \
written rather than rewriting it.
- No visible title: write a short description as the title instead.
- Illegible or cut-off text: make a reasonable guess and move on rather than dwelling \
on it -- never leave the JSON unfinished because one word was unclear.
"""


def _extract_json_object(text: str) -> dict | None:
    """qwen3-vl (a thinking model) often wraps its answer in prose or a markdown code
    fence despite being told not to -- pull out the first balanced {...} block rather
    than assuming the whole response is clean JSON."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def analyze_recipe_photo(bridge, image_bytes: bytes) -> dict:
    """Returns a draft recipe dict on success: {"parsed": True, "title", "servings",
    "ingredients", "steps"}. On any failure (job error, unparseable response) returns
    {"parsed": False, "raw_text": ..., "error": ...} -- the caller shows this back to the
    owner rather than silently discarding whatever the model actually said, consistent
    with how a failed vision read is handled everywhere else in this codebase (an honest
    "couldn't read this" beats a confidently wrong guess).
    """
    b64 = base64.b64encode(image_bytes).decode()
    # The vision route's shared default (num_predict=4096) was tuned against a simple
    # tagging task and proved too tight here: a real test of this exact prompt hit that
    # cap mid-reasoning before emitting any JSON. A recipe's ingredients+steps can
    # legitimately run long, so both are raised well past the image's own ~4000-token
    # cost plus this prompt, with headroom for the reasoning pass on top of the answer.
    job = bridge.run_sync(
        "kitchen", "vision", RECIPE_PROMPT, images=[b64],
        options={"num_predict": 8192, "num_ctx": 24576},
    )

    if job.get("status") != "done":
        return {"parsed": False, "raw_text": "", "error": job.get("error") or f"job status: {job.get('status')}"}

    raw_text = job.get("result") or ""
    parsed = _extract_json_object(raw_text)
    if parsed is None:
        return {"parsed": False, "raw_text": raw_text, "error": "could not find a valid JSON object in the response"}

    ingredients = parsed.get("ingredients")
    steps = parsed.get("steps")
    if not isinstance(ingredients, list) or not isinstance(steps, list) or not ingredients or not steps:
        return {"parsed": False, "raw_text": raw_text, "error": "response was missing ingredients or steps"}

    return {
        "parsed": True,
        "title": (parsed.get("title") or "Untitled recipe").strip(),
        "servings": parsed.get("servings") if isinstance(parsed.get("servings"), int) else None,
        "ingredients": [
            {
                "name": str(i.get("name", "")).strip(),
                "quantity": str(i.get("quantity", "")).strip(),
                "unit": str(i.get("unit", "")).strip(),
            }
            for i in ingredients if isinstance(i, dict) and i.get("name")
        ],
        "steps": [str(s).strip() for s in steps if str(s).strip()],
    }
