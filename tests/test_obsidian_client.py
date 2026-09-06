"""Verifies the ObsidianClient's core safety property: an existing note is never
overwritten — write_note always appends under a new dated section instead."""
import pytest

from assistant.core.obsidian_client import ObsidianClient


@pytest.fixture
def client(tmp_path):
    return ObsidianClient(str(tmp_path))


def _index_text(client, folder):
    return (client.vault_path / folder / "_Index.md").read_text(encoding="utf-8")


def test_write_note_creates_new_file_with_frontmatter(client):
    result = client.write_note("01-Research", "Widget Sourcing", "First pass on suppliers.")

    assert result["new"] is True
    path = client.vault_path / "01-Research" / "Widget Sourcing.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "created:" in text
    assert "# Widget Sourcing" in text
    assert "First pass on suppliers." in text


def test_write_note_to_existing_title_appends_not_overwrites(client):
    client.write_note("01-Research", "Widget Sourcing", "First pass on suppliers.")
    result = client.write_note("01-Research", "Widget Sourcing", "Found a better supplier.")

    assert result["new"] is False
    text = (client.vault_path / "01-Research" / "Widget Sourcing.md").read_text(encoding="utf-8")
    assert "First pass on suppliers." in text  # original content survives
    assert "Found a better supplier." in text  # new content appended


def test_new_note_gets_added_to_folder_index(client):
    client.write_note("00-About Me", "_Index", "placeholder")  # doesn't count as a real note title collision
    # Seed a real index like scaffold_vault.py would.
    index_path = client.vault_path / "02-Projects" / "_Index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("# Projects\n\n## Notes\n\n*(empty — nothing yet)*\n", encoding="utf-8")

    client.write_note("02-Projects", "Jarvis Vault Rollout", "Getting this set up.")

    index_text = _index_text(client, "02-Projects")
    assert "[[Jarvis Vault Rollout]]" in index_text
    assert "*(empty" not in index_text


def test_read_note_returns_error_for_missing_note(client):
    result = client.read_note("01-Research", "Nonexistent")
    assert "error" in result


def test_search_notes_matches_title_and_body(client):
    client.write_note("01-Research", "Coffee Roasters", "Notes on local coffee roasters near me.")
    client.write_note("01-Research", "Unrelated Topic", "Nothing to do with beverages.")

    result = client.search_notes("coffee")

    titles = [n["title"] for n in result["notes"]]
    assert "Coffee Roasters" in titles
    assert "Unrelated Topic" not in titles


def test_list_notes_scoped_to_folder_excludes_index_files(client):
    client.write_note("03-Areas", "Health", "Some notes.")
    client.write_note("01-Research", "Other Folder Note", "Shouldn't show up when scoped.")

    result = client.list_notes("03-Areas")

    titles = [n["title"] for n in result["notes"]]
    assert titles == ["Health"]


def test_call_tool_dispatches_by_name(client):
    result = client.call_tool("write_note", {"folder": "04-Journal", "title": "Entry", "content": "Body text."})
    assert result["new"] is True

    read_back = client.call_tool("read_note", {"folder": "04-Journal", "title": "Entry"})
    assert "Body text." in read_back["content"]

    assert "error" in client.call_tool("bogus_tool", {})
