"""Read/write access to Jarvis's Obsidian vault — its 'second brain' for research and
personal context about the user. Pure filesystem I/O (Obsidian itself doesn't need to
be running): notes are plain markdown files with YAML frontmatter, organized PARA-style
(Projects/Areas/Resources.../Archive) plus a dedicated 'About Me' folder. Exposes
call_tool(name, arguments) so engine.py can dispatch to this exactly like Era/phone/mail.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

# The owner's own PARA folders, plus one for agent-written material. This list is the
# single source of truth for the whole codebase -- engine.OBSIDIAN_FOLDERS (the enum the
# model's write_note/read_note tools are constrained to) is an alias for it, because two
# hand-maintained copies of the same list is exactly how a folder ends up writable by code
# and invisible to the model, or vice versa.
#
# 06-Agents is where everything Jarvis's agents write lands: research briefs, design
# documents, employee journals. It is deliberately separate from 01-Research/02-Projects/
# 03-Areas, which hold the owner's own hand-written notes -- his explicit instruction. He
# should never have to wonder whether he wrote a note or a machine did.
AGENT_FOLDER = "06-Agents"

FOLDERS = ["00-About Me", "01-Research", "02-Projects", "03-Areas", "04-Journal",
           "05-Archive", AGENT_FOLDER]

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
_EMPTY_PLACEHOLDER = re.compile(r"\n\*\(empty[^\n]*\)\*\n")

# An _Index.md is a file the owner actually reads, and _add_to_index appends one line per
# new note with no upper bound. The crypto desk alone creates ~2 notes a day, and the
# research-brief and employee-journal writes added alongside this push it higher -- left
# alone that is ~730+ lines a year in a file whose whole job is to be skimmable. Past this
# many links the oldest are folded away into a dated archive note; nothing is deleted.
INDEX_MAX_LINKS = 200
INDEX_KEEP_ON_COMPACT = 100


def _safe_filename(title: str) -> str:
    return _INVALID_FILENAME_CHARS.sub("-", title).strip() or "untitled"


class ObsidianClient:
    def __init__(self, vault_path: str):
        self.vault_path = Path(vault_path)

    def _note_path(self, folder: str, title: str) -> Path:
        return self.vault_path / folder / f"{_safe_filename(title)}.md"

    def note_path(self, folder: str, title: str) -> Path:
        """Where a note lives on disk. Public because housekeeping code has to rewrite a
        note in place -- write_note below is append-only on purpose, which is right for
        capture and wrong for a bounded, re-read summary (see crypto_journal.compact_summary).
        Not reachable from call_tool, so this stays code-only: an agent still only ever
        appends."""
        return self._note_path(folder, title)

    def write_note(self, folder: str, title: str, content: str, tags: list[str] | None = None) -> dict:
        folder_dir = self.vault_path / folder
        folder_dir.mkdir(parents=True, exist_ok=True)
        path = self._note_path(folder, title)
        now = datetime.now(timezone.utc).isoformat()
        is_new = not path.exists()

        if is_new:
            tag_line = ", ".join(tags or [])
            frontmatter = f"---\ntags: [{tag_line}]\ncreated: {now}\nupdated: {now}\n---\n\n"
            path.write_text(f"{frontmatter}# {title}\n\n{content}\n", encoding="utf-8")
            self._add_to_index(folder, title)
        else:
            # Append-only: an existing note is never overwritten. New material lands under
            # a dated subheading so a growing note reads as a timeline, not lost history.
            existing = path.read_text(encoding="utf-8")
            existing = re.sub(r"(?m)^updated: .*$", f"updated: {now}", existing, count=1)
            path.write_text(f"{existing}\n\n## {now[:10]}\n\n{content}\n", encoding="utf-8")

        return {"ok": True, "path": str(path.relative_to(self.vault_path)), "new": is_new}

    def _add_to_index(self, folder: str, title: str) -> None:
        index_path = self.vault_path / folder / "_Index.md"
        if not index_path.exists():
            return
        text = index_path.read_text(encoding="utf-8")
        text = _EMPTY_PLACEHOLDER.sub("\n", text)
        link_line = f"- [[{title}]]\n"
        if link_line not in text:
            text = text if text.endswith("\n") else text + "\n"
            text += link_line
        text = self._compact_index(folder, text)
        index_path.write_text(text, encoding="utf-8")

    def _compact_index(self, folder: str, text: str) -> str:
        """Keep an index skimmable by folding its oldest links into a dated archive note.

        Returns the (possibly rewritten) index body. Below the threshold this is a cheap
        line count and the text comes back untouched, so it costs nothing on the common
        path.

        Nothing is lost: the folded links are written to 05-Archive as a real note and the
        index keeps a wikilink to it, so the full history is one click away and still
        reachable by search_notes. This is the same bounded-by-construction reasoning as
        crypto_journal.compact_summary -- an append-only structure that something reads
        regularly needs a housekeeping path, or it eventually stops being readable.
        """
        lines = text.split("\n")
        link_idx = [i for i, line in enumerate(lines) if line.startswith("- [[")]
        if len(link_idx) <= INDEX_MAX_LINKS:
            return text

        fold_idx = link_idx[:-INDEX_KEEP_ON_COMPACT]
        folded = [lines[i] for i in fold_idx]
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        archive_title = f"{folder} Index Archive -- {stamp}"
        # write_note (not a raw write) so a same-day second compaction appends under a new
        # dated heading rather than clobbering the first one.
        self.write_note(
            "05-Archive", archive_title,
            f"{len(folded)} older links folded out of `{folder}/_Index.md` to keep it "
            f"readable. Every note below still exists in `{folder}`.\n\n" + "\n".join(folded),
            tags=["index", "archive"],
        )

        fold_set = set(fold_idx)
        kept = [line for i, line in enumerate(lines) if i not in fold_set]
        marker = f"- _(older entries: [[{archive_title}]])_"
        insert_at = min(link_idx[-INDEX_KEEP_ON_COMPACT:]) - len(fold_idx)
        kept.insert(max(insert_at, 0), marker)
        return "\n".join(kept)

    def read_note(self, folder: str, title: str) -> dict:
        path = self._note_path(folder, title)
        if not path.exists():
            return {"error": f"no note titled '{title}' in {folder}"}
        return {"folder": folder, "title": title, "content": path.read_text(encoding="utf-8")}

    def list_notes(self, folder: str | None = None) -> dict:
        base = (self.vault_path / folder) if folder else self.vault_path
        if not base.exists():
            return {"notes": []}
        notes = [
            {"folder": str(p.parent.relative_to(self.vault_path)), "title": p.stem}
            for p in sorted(base.rglob("*.md"))
            if p.stem not in ("_Index", "Index")
        ]
        return {"notes": notes}

    def search_notes(self, query: str, limit: int = 10) -> dict:
        query_lower = query.lower()
        matches = []
        for p in sorted(self.vault_path.rglob("*.md")):
            if p.stem in ("_Index", "Index"):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            idx = text.lower().find(query_lower)
            if idx < 0 and query_lower not in p.stem.lower():
                continue
            snippet = text[max(0, idx - 80): idx + 160].replace("\n", " ").strip() if idx >= 0 else ""
            matches.append({"folder": str(p.parent.relative_to(self.vault_path)), "title": p.stem, "snippet": snippet})
            if len(matches) >= limit:
                break
        return {"notes": matches}

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name == "write_note":
            return self.write_note(arguments["folder"], arguments["title"], arguments["content"], arguments.get("tags"))
        if name == "read_note":
            return self.read_note(arguments["folder"], arguments["title"])
        if name == "list_notes":
            return self.list_notes(arguments.get("folder"))
        if name == "search_notes":
            return self.search_notes(arguments["query"], arguments.get("limit", 10))
        return {"error": f"unknown obsidian tool {name}"}
