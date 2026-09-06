"""Read/write access to Jarvis's Obsidian vault — its 'second brain' for research and
personal context about the user. Pure filesystem I/O (Obsidian itself doesn't need to
be running): notes are plain markdown files with YAML frontmatter, organized PARA-style
(Projects/Areas/Resources.../Archive) plus a dedicated 'About Me' folder. Exposes
call_tool(name, arguments) so engine.py can dispatch to this exactly like Era/phone/mail.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

FOLDERS = ["00-About Me", "01-Research", "02-Projects", "03-Areas", "04-Journal", "05-Archive"]

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
_EMPTY_PLACEHOLDER = re.compile(r"\n\*\(empty[^\n]*\)\*\n")


def _safe_filename(title: str) -> str:
    return _INVALID_FILENAME_CHARS.sub("-", title).strip() or "untitled"


class ObsidianClient:
    def __init__(self, vault_path: str):
        self.vault_path = Path(vault_path)

    def _note_path(self, folder: str, title: str) -> Path:
        return self.vault_path / folder / f"{_safe_filename(title)}.md"

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
        index_path.write_text(text, encoding="utf-8")

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
