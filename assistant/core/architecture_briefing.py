"""The house's own architecture, put in front of the systems engineers who are supposed
to know it.

Mirrors agent_policy.py's shape exactly -- pure retrieval, no new store, nothing written
back to the vault, one more text block spliced into a scheduled prompt -- for the same
reason: scheduled staff are one-shot completions with no mid-run tool loop, so knowledge
has to arrive pre-fetched or not at all.

The gap this closes is specific. Jack designed the sys-admin monitoring feature with a
past session, in detail, and it sat in that session's own memory -- never in the vault
Jarvis actually reads from. When he asked Jarvis directly for it, the honest answer was
"no requirements doc exists," which was true of the vault and false of what had actually
been decided. A design that only ever lived in a chat transcript is invisible to every
future employee; this feed is the fix for that class of problem, not just today's
instance of it.

A read failure degrades to a stated absence, never an exception -- same rule as
agent_policy.py, for the same reason.
"""
import logging

logger = logging.getLogger(__name__)

# (folder, title) of every note that describes how the house is actually built. Explicit,
# not discovered by scanning a folder -- same reasoning as agent_policy.POLICY_NOTES.
ARCHITECTURE_NOTES = (
    ("03-Areas", "Jarvis Architecture"),
)

ARCHITECTURE_CONTEXT_CHARS = 2000
MIN_USEFUL_SHARE = 120


def _strip_frontmatter(text: str, title: str = "") -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            text = text[end + 4:]
    text = text.strip()
    if title:
        heading = f"# {title}"
        if text.startswith(heading):
            text = text[len(heading):].lstrip()
    return text


def _head(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...(this note continues past what you were shown)"


def _allocate(sizes: list[int], budget: int) -> list[int]:
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    out = [0] * len(sizes)
    remaining, left = budget, len(sizes)
    for i in order:
        share = remaining // max(left, 1)
        take = min(sizes[i], share)
        out[i] = take
        remaining -= take
        left -= 1
    return out


def build_architecture_briefing(obsidian, max_chars: int = ARCHITECTURE_CONTEXT_CHARS) -> str:
    """Background knowledge of how the house is built, pre-fetched into this run's
    prompt. Returns "" when there's no vault client, so an employee holding the feed on a
    deployment without one runs normally."""
    if obsidian is None:
        return ""

    bodies, unreadable = [], []
    for folder, title in ARCHITECTURE_NOTES:
        try:
            note = obsidian.read_note(folder, title)
        except Exception:
            logger.exception("could not read the architecture note %s/%s", folder, title)
            unreadable.append(title)
            continue
        if not isinstance(note, dict) or note.get("error"):
            logger.warning("architecture note not found in the vault: %s/%s", folder, title)
            unreadable.append(title)
            continue
        body = _strip_frontmatter(note.get("content") or "", title)
        if body:
            bodies.append((title, body))

    if not bodies:
        if unreadable:
            return ("\n\n--- HOUSE ARCHITECTURE ---\nNo architecture note could be read "
                    "from the vault this run (" + ", ".join(unreadable) + "). Say plainly "
                    "that you don't have written architecture knowledge yet rather than "
                    "guessing at how something is built.\n--- end architecture ---\n")
        return ""

    shares = _allocate([len(b) for _, b in bodies], max_chars)
    parts = [
        "\n\n--- HOUSE ARCHITECTURE, background knowledge ---",
        "How this house is actually built, in the owner's own words. Treat it as working "
        "knowledge you should be able to recite, not a document to summarize back to him.",
    ]
    for (title, body), share in zip(bodies, shares):
        if share < len(body) and share < MIN_USEFUL_SHARE:
            continue
        parts.append(f"\n## {title}\n{_head(body, share)}")
    if unreadable:
        parts.append("\nNote: these notes could not be read this run: " + ", ".join(unreadable) + ".")
    parts.append("--- end architecture ---\n")
    return "\n".join(parts)
