"""Covers core/storage_plan.py: where each folder is planned to go, that a copy already on
the destination drive is the one kept, that held and unsorted folders are never planned,
and that approvals survive a rebuild."""
import json

import pytest

from assistant.core import drive_inventory as inv
from assistant.core import storage_plan as sp

MB = 1 << 20


def scan(db, host, mount, entries, uid, label="L"):
    hid = inv.upsert_host(db, host, "1", "windows")
    vid = inv.upsert_volume(db, hid, uid, mount, label=label)
    w = inv.ScanWriter(db, vid, inv.start_scan(db, vid))
    for path, size in entries:
        w.add("f", size, 1.6e9, path)
    w.finish()
    return vid


def tag(db, vid, path, cat):
    with inv.connect(db) as c:
        d = c.execute("SELECT d.id FROM inv_dirs d JOIN inv_volumes v ON v.current_scan_id = d.scan_id "
                      "WHERE v.id = ? AND d.path = ?", (vid, path)).fetchone()
    inv.set_tag(db, d["id"], cat)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "inventory.db")
    sp.init_db(path)
    d = scan(path, "jarvisbox", "D:\\", [("Collected Art/Etsy/logo.svg", 3 * MB)], "D")
    scan(path, "jarvisbox", "E:\\", [], "E")
    f = scan(path, "jarvisbox", "F:\\", [
        ("CleanedUp/Halloween Bundle/SVG/logo.svg", 3 * MB),      # same bytes as D's copy
        ("CleanedUp/Halloween Bundle/SVG/bat.svg", 2 * MB),
        ("CleanedUp/Halloween Bundle/PNG/bat.png", 5 * MB),
        ("GoPro/100GOPRO/GX010001.MP4", 50 * MB),
        ("Mystery/stuff.bin", 200 * MB),
    ], "F")
    h = scan(path, "jarvisbox", "H:\\", [("virtualMachines/disk.mp4", 900 * MB)], "H")
    for vid, p in ((d, "Collected Art"), (f, "CleanedUp")):
        tag(path, vid, p, "side_hustle")
    tag(path, f, "GoPro", "home_video")
    tag(path, h, "virtualMachines", "home_video")
    inv.record_hashes(path, "D", [("Collected Art/Etsy/logo.svg", 3 * MB, 1.6e9, "same")])
    inv.record_hashes(path, "F", [("CleanedUp/Halloween Bundle/SVG/logo.svg", 3 * MB, 1.6e9, "same")])
    return path


def plan_by_path(db):
    sp.build(db)
    with inv.connect(db) as c:
        return {r["source_path"]: dict(r) for r in c.execute("SELECT * FROM plan_units")}


def test_side_hustle_is_merged_by_type_and_generic_folders_keep_their_bundle_name(db):
    p = plan_by_path(db)
    assert p["CleanedUp/Halloween Bundle/SVG"]["dest_path"] == "Side Hustle/SVG & Cut Files/Halloween Bundle - SVG"
    assert p["CleanedUp/Halloween Bundle/PNG"]["dest_path"] == "Side Hustle/Images/Halloween Bundle - PNG"


def test_the_copy_already_on_the_destination_is_kept_and_not_copied_again(db):
    p = plan_by_path(db)
    svg = p["CleanedUp/Halloween Bundle/SVG"]
    assert (svg["dup_files"], svg["dup_bytes"], svg["new_files"]) == (1, 3 * MB, 1)
    assert p["Collected Art/Etsy"]["same_drive"] == 1 and p["Collected Art/Etsy"]["dup_files"] == 0


def test_personal_media_moves_as_a_whole_tree(db):
    p = plan_by_path(db)
    assert p["GoPro"]["kind"] == "tree" and p["GoPro"]["dest_path"] == "Personal/Home Video/GoPro"
    assert "GoPro/100GOPRO" not in p


def test_held_and_unsorted_folders_are_never_planned(db):
    p = plan_by_path(db)
    assert not any(k.startswith("virtualMachines") for k in p)
    assert "Mystery" not in p
    needs = {n["source_path"]: n["reason"] for n in sp.summary(db)["needs_decision"]}
    assert needs["virtualMachines"].startswith("held") and needs["Mystery"].startswith("unsorted")


def test_approvals_survive_a_rebuild(db):
    p = plan_by_path(db)
    sp.set_status(db, "approved", ids=[p["GoPro"]["id"]])
    p = plan_by_path(db)
    assert p["GoPro"]["status"] == "approved"
    assert p["CleanedUp/Halloween Bundle/PNG"]["status"] == "proposed"


@pytest.mark.parametrize("names,expected", [
    (["Bundle", "SVG"], "Bundle - SVG"),
    (["Shop", "Bundle", "Commercial Use", "PNG"], "Bundle - Commercial Use - PNG"),
    (["Nice Name"], "Nice Name"),
    (["x", 'bad:name?'], "bad_name_"),
])
def test_display_name(names, expected):
    assert sp.display_name(names) == expected


def _u(path, kind, section, b=10):
    return {"source_uid": "V", "source_path": path, "kind": kind, "section": section, "bytes": b, "new_bytes": b}


def test_a_project_moves_intact_and_nothing_inside_it_is_planned_separately():
    units = sp.resolve_nesting([
        _u("arduino", "tree", sp.PROJECTS, 100),
        _u("arduino/saved_desktop_data", "tree", sp.PERSONAL),
        _u("arduino/lib/art", "files", sp.SIDE_HUSTLE),
    ])
    assert [u["source_path"] for u in units] == ["arduino"]


def test_a_personal_tree_skips_what_is_carved_out_of_it():
    units = {u["source_path"]: u for u in sp.resolve_nesting([
        _u("Old Laptop", "tree", sp.PERSONAL, 100),
        _u("Old Laptop/Etsy art", "files", sp.SIDE_HUSTLE, 30),
        _u("Old Laptop/code", "tree", sp.PROJECTS, 20),
        _u("Old Laptop/code/assets", "files", sp.SIDE_HUSTLE, 5),
    ])}
    assert set(units) == {"Old Laptop", "Old Laptop/Etsy art", "Old Laptop/code"}   # project stays intact
    ex = {e["path"] for e in json.loads(units["Old Laptop"]["excludes"])}
    assert ex == {"Old Laptop/Etsy art", "Old Laptop/code"}
    assert units["Old Laptop"]["bytes"] == 50


def test_live_paths_come_from_jarvis_db_and_obsidian(tmp_path):
    import sqlite3
    jdb = str(tmp_path / "jarvis.db")
    with sqlite3.connect(jdb) as c:
        c.execute("CREATE TABLE design_assets (path TEXT)")
        c.execute(r"INSERT INTO design_assets VALUES ('D:\Jarvis Generated\artwork\a.jpg')")
        c.execute("CREATE TABLE media_copies (dest_path TEXT)")             # old queue history: ignored
        c.execute(r"INSERT INTO media_copies VALUES ('D:\Collected Art\x.zip')")
    obs = tmp_path / "obsidian.json"
    obs.write_text(json.dumps({"vaults": {"a": {"path": r"D:\Obsidian\Jarvis"}}}))
    assert sp.live_paths(jdb, str(obs)) == {("D:\\", "Jarvis Generated"), ("D:\\", "Obsidian")}


def test_protected_folders_and_code_projects(tmp_path):
    db = str(tmp_path / "inventory.db")
    sp.init_db(db)
    d = scan(db, "jarvisbox", "D:\\", [
        ("Obsidian/Jarvis/note.md", 1000),
        ("Vinyl Stuff/.vscode/settings.json", 10),
        ("Vinyl Stuff/server/index.js", 10),
        ("Vinyl Stuff/library/ANGELS/angel.svg", 10),
        ("Collected Art/Etsy/logo.svg", 10),
    ], "D")
    scan(db, "jarvisbox", "E:\\", [], "E")
    for p in ("Obsidian", "Vinyl Stuff", "Collected Art"):
        tag(db, d, p, "side_hustle")
    sp.build(db, protected={("D:\\", "Obsidian")})
    with inv.connect(db) as c:
        units = {r["source_path"]: dict(r) for r in c.execute("SELECT * FROM plan_units")}
    assert not any(p.startswith("Obsidian") for p in units)
    assert units["Vinyl Stuff"]["kind"] == "tree"
    assert units["Vinyl Stuff"]["dest_path"] == "Side Hustle/Software & Code/Vinyl Stuff"
    assert not any(p.startswith("Vinyl Stuff/") for p in units)       # nothing carved out of it
    assert units["Collected Art/Etsy"]["kind"] == "files"


def test_same_named_projects_get_their_own_folders(tmp_path):
    db = str(tmp_path / "inventory.db")
    sp.init_db(db)
    scan(db, "jarvisbox", "D:\\", [], "D")
    e = scan(db, "jarvisbox", "E:\\", [], "E")
    f = scan(db, "jarvisbox", "F:\\", [("A/CrimeCameraClient/package.json", 500), ("B/CrimeCameraClient/package.json", 10)], "F")
    for p in ("A", "B"):                       # e.g. two old laptops' video folders
        tag(db, f, p, "home_video")
    for p in ("A/CrimeCameraClient", "B/CrimeCameraClient"):
        tag(db, f, p, "projects")
    sp.build(db)
    with inv.connect(db) as c:
        dests = sorted(r[0] for r in c.execute("SELECT dest_path FROM plan_units WHERE kind = 'tree' AND section = 'Projects'"))
    assert dests == ["Projects/CrimeCameraClient", "Projects/CrimeCameraClient (2)"]


def test_a_media_only_copy_of_a_known_project_is_not_split_by_type(tmp_path):
    db = str(tmp_path / "inventory.db")
    sp.init_db(db)
    d = scan(db, "jarvisbox", "D:\\", [
        ("Collected Art/Stickers/CustomVinylRetail-main (1)/web/images/icon.png", 10),   # media-only copy
        ("Collected Art/Etsy/logo.png", 10),
    ], "D")
    scan(db, "jarvisbox", "E:\\", [], "E")
    k = scan(db, "pi5nas002", "/mnt/kingston_ssd", [("Downloads/CustomVinylRetail-main/package.json", 10),
                                                    ("Downloads/CustomVinylRetail-main/web/images/icon.png", 10)], "K")
    tag(db, d, "Collected Art", "side_hustle")
    tag(db, k, "Downloads", "side_hustle")
    sp.build(db)
    with inv.connect(db) as c:
        paths = {r[0] for r in c.execute("SELECT source_path FROM plan_units")}
    assert "Collected Art/Etsy" in paths
    assert not any("CustomVinylRetail-main (1)" in p for p in paths)
    assert "Downloads/CustomVinylRetail-main" in paths                    # the real project moves whole
    needs = {n["source_path"] for n in sp.summary(db)["needs_decision"]}
    assert "Collected Art/Stickers/CustomVinylRetail-main (1)" in needs


def test_interrupted_and_failed_units_stay_approved_across_a_rebuild(db):
    p = plan_by_path(db)
    with inv.connect(db) as c:
        c.execute("UPDATE plan_units SET status = 'running' WHERE id = ?", (p["GoPro"]["id"],))
        c.execute("UPDATE plan_units SET status = 'failed' WHERE id = ?", (p["CleanedUp/Halloween Bundle/PNG"]["id"],))
        c.commit()
    p = plan_by_path(db)
    assert p["GoPro"]["status"] == "approved"
    assert p["CleanedUp/Halloween Bundle/PNG"]["status"] == "approved"
