"""Jarvis's own read-back: his standing-preference notes, in his system prompt.

Until this existed, ZERO bytes of the vault reached Jarvis's own prompt. `read_note` and
`search_notes` were purely on-demand tools, which means they only fire when something in
the conversation makes him think to look -- and a standing preference is exactly the kind
of thing he would never think to look up, because he does not know it exists. The owner
has written down, in his own words, that he wants short answers and no partial status
reports, and Jarvis has been giving him long ones and reading none of it.

These six notes total about 4KB today:

    00-About Me/Standing Preferences
    00-About Me/Standing Rules
    00-About Me/Working Standards - Jarvis Conduct
    00-About Me/Assistant Operating Preferences
    00-About Me/Communication Preferences
    00-About Me/Communication Preference — Status Reports

READ ONCE AT STARTUP, CACHED FOREVER. THIS IS THE WHOLE DESIGN.
---------------------------------------------------------------
`build_system_prompt`'s own docstring spells out why, and it is worth restating because
getting this wrong would make the feature actively harmful. The Claude CLI backend
prompt-caches on an exact prefix match. Anything that varies per turn invalidates that
cache and re-bills the ~25k-token Claude Code preamble on EVERY SINGLE MESSAGE. It is the
same reasoning that deliberately keeps the current time out of the system prompt.

A vault digest re-read per turn would be the most expensive possible way to implement
memory: ~4KB of value bought at ~25k tokens of waste per message, forever. So the digest
is built exactly once, in setup.build_obsidian_context, and handed to every later prompt
as a fixed string. The cost is that editing a note needs a service restart to take effect.
That is the right trade for notes the owner changes a few times a month, and it is stated
here so nobody "fixes" it later by making it live.

The digest is also a deliberately STATIC selection -- a fixed list of note titles, not a
search. A search would vary with the conversation, which reintroduces exactly the
per-turn variance this is built to avoid.
"""
import logging

logger = logging.getLogger(__name__)

# Fixed and explicit. These are the notes about how to BE his assistant -- not his
# research, not his projects, not his personal life. 00-About Me also holds notes on his
# family, his health and a rental dispute; none of that belongs in every prompt, and a
# "read the whole folder" rule would put it there.
STANDING_NOTES = (
    ("00-About Me", "Standing Preferences"),
    ("00-About Me", "Standing Rules"),
    ("00-About Me", "Working Standards - Jarvis Conduct"),
    ("00-About Me", "Assistant Operating Preferences"),
    ("00-About Me", "Communication Preferences"),
    ("00-About Me", "Communication Preference — Status Reports"),
)

# ~4KB of notes today. The cap is a guard against one note growing without anyone
# noticing, not a target -- unlike the per-run agent feeds, this is paid once per process,
# so there is no reason to trim aggressively.
DIGEST_CHARS = 6000
PER_NOTE_CHARS = 1200


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            text = text[end + 4:]
    return text.strip()


def build_digest(obsidian, max_chars: int = DIGEST_CHARS,
                 per_note: int = PER_NOTE_CHARS) -> str:
    """Read the standing notes and render the prompt block. Call ONCE, at startup.

    Returns "" when there is no vault or nothing readable, so a missing vault costs Jarvis
    the digest and nothing else. Never raises: a bad drive letter must not take down the
    assistant, the same defensive posture build_obsidian_context already takes.
    """
    if obsidian is None:
        return ""

    sections = []
    used = 0
    for folder, title in STANDING_NOTES:
        if used >= max_chars:
            break
        try:
            note = obsidian.read_note(folder, title)
        except Exception:
            logger.exception("could not read the standing note %s/%s", folder, title)
            continue
        if not isinstance(note, dict) or note.get("error"):
            # Not a warning: these are hand-maintained and he may simply not have written
            # one. A missing note here is an absence, not a fault.
            logger.debug("standing note not present in the vault: %s/%s", folder, title)
            continue
        body = _strip_frontmatter(note.get("content") or "")
        if not body:
            continue
        excerpt = body[: min(per_note, max_chars - used)]
        sections.append(f"## {title}\n{excerpt}")
        used += len(excerpt)

    if not sections:
        return ""

    logger.info("standing preferences digest: %d note(s), %d chars, cached for this process",
                len(sections), used)
    return (
        "\n\nSTANDING PREFERENCES — the user wrote these himself, in his own vault. They "
        "are how he wants you to behave, they apply to every conversation, and they "
        "outrank your own defaults about tone, length and thoroughness. Follow them "
        "without being reminded and without mentioning that you read them.\n\n"
        + "\n\n".join(sections)
        + "\n--- end standing preferences ---\n"
    )
