"""Local-first fast path for simple Home Assistant commands: tries a local Ollama
model before ever reaching the cloud backend, since a real live-traced example showed
the actual bottleneck for "turn on a light" is model round-trip latency, not Home
Assistant itself -- HA's own calls are consistently sub-second (confirmed live). Falls
back to the normal (cloud) path for anything the local model doesn't handle with real
confidence: a wrong guess on a smart-home command is a worse outcome than a few extra
seconds of latency, so this only ever acts on exactly one well-formed tool call against
a known tool, and declines (returns None) otherwise.

Deliberately narrow for now (Home Assistant only, at most one tool call, non-sensitive
domains only), but the owner's actual plan is a second, dedicated local Ollama server
that eventually becomes Jarvis's main LLM -- this fast path is where that migration's
rough edges (tool-calling reliability on a small local model, keeping it warm, what
"confident enough" means) get worked out on a narrow, low-stakes slice of real traffic
first, before anything is trusted to run everything.
"""
import json
import logging

logger = logging.getLogger(__name__)

# Entities outside these domains (mainly sensor./binary_sensor., which can number in the
# hundreds in a real house) are dropped from the prompt -- the fast path only ever needs
# to name something it might actually be asked to act on or report, and a smaller prompt
# is also a faster one.
_RELEVANT_DOMAINS = {
    "light", "switch", "climate", "lock", "cover", "fan", "media_player",
    "alarm_control_panel", "weather",
}

SYSTEM_PROMPT_TEMPLATE = """You are Jarvis's fast local path for simple Home Assistant commands. You have exactly ONE turn: call the one tool that satisfies the request, or don't call anything if you're not sure. Do not explain, do not ask a follow-up question -- another, more capable system handles anything you don't confidently resolve here.

Rules:
- If the request names or implies MORE THAN ONE device for the same service, call the tool ONCE with entity_id as a list of every target -- never call it more than once.
- Only use entity ids from the list below. Never guess or invent one.
- If you are not confident which entity(ies) the request means, or the request needs more than one tool call, or it isn't really a Home Assistant request at all, make NO tool call.

Known entities (domain.name -- friendly name -- current state):
{entities}
"""


def _format_entities(entities: list[dict]) -> str:
    lines = [
        f"{e['entity_id']} -- {e.get('name', e['entity_id'])} -- {e.get('state', '?')}"
        for e in entities if e["entity_id"].split(".", 1)[0] in _RELEVANT_DOMAINS
    ]
    return "\n".join(lines) if lines else "(none known)"


# Common services phrased naturally; anything else falls back to a generic "done".
_SERVICE_PAST_TENSE = {
    "turn_on": "turned on", "turn_off": "turned off", "toggle": "toggled",
    "open_cover": "opened", "close_cover": "closed", "set_temperature": "set",
    "lock": "locked", "unlock": "unlocked",
}


def _deterministic_action_reply(arguments: dict) -> str:
    phrase = _SERVICE_PAST_TENSE.get(arguments.get("service"), "done")
    entity_id = arguments.get("entity_id")
    count = len(entity_id) if isinstance(entity_id, list) else 1
    if count >= 3:
        return f"All {count} {phrase}, sir."
    if count == 2:
        return f"Both {phrase}, sir."
    return f"{phrase.capitalize()}, sir."


def _extract_single_tool_call(message, sensitive_domains=()) -> tuple[str, dict] | None:
    """One (name, arguments) from a model that may have emitted several tool calls.

    Same dict-style access as engine.handle_message's own Ollama tool loop
    (message.get("tool_calls"), call["function"]) -- proven shape for this backend.

    Several calls are merged rather than refused when they are plainly one request. The
    local model is not consistent about shape: "turn the living lights on" comes back as
    one call carrying three entity_ids on one pass and as three separate calls on the
    next, and refusing the second shape sent an identical sentence down the slow path at
    random. Measured on his own history, the same request took 0.0s one night and 12.3s
    the next, which is worse than being reliably slow -- he cannot build a habit on it.

    Merging is deliberately narrow: same tool, same domain, same service, and every
    argument except entity_id identical, so "all the lights on" merges and "lights on and
    set the thermostat" still falls through to the main path to be reasoned about
    properly. A sensitive domain never merges, for the same reason it is never actioned
    here at all.
    """
    tool_calls = message.get("tool_calls")
    if not tool_calls:
        return None

    parsed: list[tuple[str, dict]] = []
    for call in tool_calls:
        fn = (call.get("function") or {}) if hasattr(call, "get") else call["function"]
        name, arguments = fn.get("name"), fn.get("arguments")
        if not name or not isinstance(arguments, dict):
            return None
        parsed.append((name, arguments))

    if len(parsed) == 1:
        return parsed[0]

    if {name for name, _ in parsed} != {"call_service"}:
        return None
    targets = {(a.get("domain"), a.get("service")) for _, a in parsed}
    if len(targets) != 1:
        return None
    domain, _service = next(iter(targets))
    if domain in sensitive_domains:
        return None
    # Everything except the entities must agree, or merging would silently apply one
    # call's brightness or colour to entities the model did not ask it for.
    rest = {tuple(sorted((k, repr(v)) for k, v in a.items() if k != "entity_id"))
            for _, a in parsed}
    if len(rest) != 1:
        return None

    entities: list = []
    for _, a in parsed:
        got = a.get("entity_id")
        entities.extend(got if isinstance(got, list) else [got] if got else [])
    if not entities:
        return None
    seen = set()
    merged = dict(parsed[0][1])
    merged["entity_id"] = [e for e in entities if not (e in seen or seen.add(e))]
    return "call_service", merged


def try_home_assistant_fast_path(
    local_llm, home_assistant, db_path: str, requesting_user_id: int, user_text: str,
    tz_name: str = "UTC",
) -> str | None:
    """Attempts to fully handle one Home-Assistant-flavored message locally. Returns the
    final reply on success, or None to signal "not handled -- fall back to the normal
    path unchanged", which is always safe as long as this returns None before any real
    tool has been dispatched. Once a real (non-sensitive) action has actually been
    dispatched, this never returns None again -- falling back after a real side effect
    already happened risks the main backend deciding the same thing over again and
    double-executing it, so from that point on it always returns some reply, using a
    plain deterministic one if the local model can't even phrase a nicer one.
    """
    if local_llm is None or home_assistant is None:
        return None

    # Deferred imports: engine.py needs to import this module inside handle_message,
    # and this needs engine.py's tool catalogue and dispatcher -- a module-level import
    # either direction would be circular. Both modules are fully loaded by the time any
    # message is actually handled, so this is safe (same pattern staff.py's assign()
    # already uses for its own deferred imports).
    from .engine import HOME_ASSISTANT_KEYWORDS, HOME_ASSISTANT_TOOLS, _dispatch_tool_call

    text = user_text.lower()
    if not any(kw in text for kw in HOME_ASSISTANT_KEYWORDS):
        return None

    try:
        entities = home_assistant.mcp_client.list_entities(None).get("entities", [])
    except Exception:
        logger.debug("HA fast path: could not fetch entities, falling back", exc_info=True)
        return None

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(entities=_format_entities(entities))},
        {"role": "user", "content": user_text},
    ]
    try:
        message = local_llm.chat(messages, tools=HOME_ASSISTANT_TOOLS, think=False)
    except Exception:
        logger.debug("HA fast path: local model call failed, falling back", exc_info=True)
        return None

    call = _extract_single_tool_call(message, home_assistant.sensitive_domains)
    if call is None:
        return None  # nothing confident enough to act on -- let the main path reason properly
    name, arguments = call
    if name not in home_assistant.tool_names:
        return None

    if name == "call_service" and arguments.get("domain") in home_assistant.sensitive_domains:
        # Never even attempt these here: the normal path's confirmation-gate phrasing
        # (a real conversational "are you sure?") is worth the extra latency for a lock
        # or the alarm, and staging a pending_action from both paths would double it up.
        return None

    try:
        result = json.loads(_dispatch_tool_call(
            db_path, tz_name, requesting_user_id, name, arguments,
            era=None, calendar=None, home_assistant=home_assistant,
        ))
    except Exception:
        logger.exception("HA fast path: dispatch raised for %s", name)
        return None
    if result.get("status") == "awaiting_confirmation" or "error" in result:
        # Nothing real happened (a race let a sensitive call through, or HA itself
        # errored) -- still safe to hand this whole message to the main path fresh.
        return None

    # Past this point a real action may have happened (or a real read completed) --
    # commit to answering, never fall through to the main path again from here.
    if name == "call_service":
        # A deterministic confirmation, not a second model call: there's no real
        # information to phrase for a pure action (it either worked or it didn't), and
        # every garbled reply seen live during testing happened on this exact
        # follow-up call -- a leaked "thought\n<channel|>...", a bare "1024", a stray
        # "100%\n" prefixed onto an otherwise fine sentence. Skipping it here removes
        # that whole failure class for the single most common case (turn a device
        # on/off) and is also strictly faster, which is the actual point of this path.
        return _deterministic_action_reply(arguments)

    messages.append(message)
    messages.append({"role": "tool", "content": json.dumps(result)})
    try:
        followup = local_llm.chat(messages, tools=None, think=False)
        reply = (followup.get("content") or "").strip()
    except Exception:
        logger.debug("HA fast path: reply phrasing failed, using a plain fallback", exc_info=True)
        reply = ""
    if not _looks_like_a_clean_reply(reply):
        # Confirmed live: this local model occasionally leaks raw formatting into
        # content -- one real run came back "thought\n<channel|>Successfully turned on
        # both kitchen stove lights." The action had already happened correctly; only
        # the phrasing was garbled. Same "never guess" principle as the tool-call
        # confidence check above, applied to reply quality: don't show the owner
        # something that looks like a formatting leak, fall back to the plain one.
        if reply:
            logger.debug("HA fast path: discarding a garbled reply: %r", reply)
        reply = f"Done, sir. ({json.dumps(result)[:200]})"
    return reply


def _looks_like_a_clean_reply(reply: str) -> bool:
    if not reply:
        return False
    # A spoken-style confirmation has no real reason to contain raw markup -- angle
    # brackets/pipes are the shape of one leak seen live ("thought\n<channel|>...").
    if any(ch in reply for ch in "<>|"):
        return False
    # A second, different leak seen live: a bare token with no markup at all ("1024",
    # in reply to "turn on the kitchen stove lights please") -- the action itself had
    # still worked correctly, only the phrasing came back nonsense. A real confirmation
    # is at least a couple of real words ("Done, sir." is two), so requiring that catches
    # this without needing to enumerate every way a small local model can misfire.
    alphabetic_words = [w for w in reply.split() if any(c.isalpha() for c in w)]
    return len(alphabetic_words) >= 2
