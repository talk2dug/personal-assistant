"""Covers drive_inventory.py: rolling totals up the folder tree, collapsing bulk noise,
suggesting categories, and tags that inherit down the tree and survive a rescan."""
import pytest

from assistant.core import drive_inventory as inv


def scan(db_path, entries, uid="UUID-1", mount="/mnt/a", is_system=False, host="box"):
    host_id = inv.upsert_host(db_path, host, "10.0.0.1", "linux")
    vol_id = inv.upsert_volume(db_path, host_id, uid, mount, label="A", is_system=is_system)
    scan_id = inv.start_scan(db_path, vol_id)
    w = inv.ScanWriter(db_path, vol_id, scan_id, is_system=is_system)
    for e in entries:
        w.add(*e)
    w.finish()
    return vol_id


def f(path, size=1000, mtime=1_600_000_000.0):
    return ("f", size, mtime, path)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "inventory.db")
    inv.init_db(path)
    return path


def dirs_by_path(db_path, vol_id):
    with inv.connect(db_path) as conn:
        scan_id = conn.execute("SELECT current_scan_id FROM inv_volumes WHERE id=?", (vol_id,)).fetchone()[0]
        return {r["path"]: dict(r) for r in conn.execute("SELECT * FROM inv_dirs WHERE scan_id=?", (scan_id,))}


def test_totals_roll_up_the_tree_and_empty_folders_still_appear(db_path):
    vol = scan(db_path, [
        f("Old Laptop/Users/Jack/Documents/taxes.pdf", 300),
        f("Old Laptop/Users/Jack/Pictures/IMG_0001.jpg", 5000),
        f("readme.txt", 10),
        ("d", 0, None, "Empty Folder"),
    ])
    d = dirs_by_path(db_path, vol)
    assert d[""]["total_bytes"] == 5310 and d[""]["total_files"] == 3
    assert d["Old Laptop"]["total_bytes"] == 5300 and d["Old Laptop"]["direct_files"] == 0
    assert d["Old Laptop/Users/Jack"]["total_dirs"] == 2
    assert "Empty Folder" in d and d["Empty Folder"]["total_files"] == 0


def test_node_modules_is_counted_but_not_itemised(db_path):
    vol = scan(db_path, [
        f("app/package.json", 100),
        f("app/node_modules/react/index.js", 7000),
        f("app/node_modules/react/lib/x.js", 3000),
    ])
    d = dirs_by_path(db_path, vol)
    assert d["app/node_modules"]["total_bytes"] == 10000
    assert d["app/node_modules"]["collapsed"] == "system_junk"
    assert "app/node_modules/react" not in d
    with inv.connect(db_path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM inv_files")}
    assert names == {"package.json"}
    assert d["app"]["suggested"] == "projects"


def test_the_live_os_is_collapsed_but_a_recovered_drives_windows_is_itemised(db_path):
    live = scan(db_path, [f("Windows/System32/a.dll", 50)], uid="C", mount="C:\\", is_system=True)
    assert dirs_by_path(db_path, live)["Windows"]["collapsed"] == "system_junk"

    recovered = scan(db_path, [f("Windows/System32/a.dll", 50)], uid="OLD", mount="E:\\")
    d = dirs_by_path(db_path, recovered)
    assert d["Windows"]["collapsed"] is None
    assert d["Windows/System32"]["suggested"] == "system_junk"


@pytest.mark.parametrize("path,expected", [
    ("DCIM/100APPLE/IMG_1234.HEIC", "photos"),
    ("Etsy Shop/Photos/listing1.jpg", "side_hustle"),      # side hustle is sticky
    ("stuff/bundle/a.svg", "side_hustle"),                  # design files by content
    ("Music/Album/01.mp3", "music"),
    ("Documents/2019/Taxes/w2.pdf", "documents"),
    ("Installers/setup.exe", "software"),
    ("random/x/VID_20190101_120000.mp4", "home_video"),
])
def test_suggestions(db_path, path, expected):
    vol = scan(db_path, [f(path, 50_000)])
    folder = path.rsplit("/", 1)[0]
    assert dirs_by_path(db_path, vol)[folder]["suggested"] == expected


def test_every_suggestion_says_why(db_path):
    vol = scan(db_path, [f("DCIM/IMG_1.jpg")])
    assert dirs_by_path(db_path, vol)["DCIM"]["suggested_reason"] == "folder name 'DCIM'"


def test_a_tag_applies_to_the_subtree_until_a_subfolder_overrides_it(db_path):
    vol = scan(db_path, [f("Recovered/a/x.jpg"), f("Recovered/b/y.pdf"), f("Recovered/b/c/z.pdf")])
    d = dirs_by_path(db_path, vol)
    inv.set_tag(db_path, d["Recovered"]["id"], "documents")
    inv.set_tag(db_path, d["Recovered/b/c"]["id"], "side_hustle")

    d = dirs_by_path(db_path, vol)
    assert (d["Recovered"]["category"], d["Recovered"]["category_source"]) == ("documents", "tag")
    assert (d["Recovered/a"]["category"], d["Recovered/a"]["category_source"]) == ("documents", "inherited_tag")
    assert d["Recovered/b/c"]["category"] == "side_hustle"

    inv.set_tag(db_path, d["Recovered"]["id"], None)
    d = dirs_by_path(db_path, vol)
    assert d["Recovered/a"]["category_source"] == "suggested"
    assert d["Recovered/b/c"]["category"] == "side_hustle"


def test_tags_survive_a_rescan_after_the_drive_changes_letter(db_path):
    vol = scan(db_path, [f("Art/a.svg")], uid="SERIAL-9", mount="E:\\")
    inv.set_tag(db_path, dirs_by_path(db_path, vol)["Art"]["id"], "photos")

    again = scan(db_path, [f("Art/a.svg"), f("Art/b.svg")], uid="SERIAL-9", mount="F:\\")

    assert again == vol
    d = dirs_by_path(db_path, again)
    assert d["Art"]["category"] == "photos" and d["Art"]["total_files"] == 2
    with inv.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM inv_files").fetchone()[0] == 2   # old scan's rows gone


def test_overview_totals_bytes_by_category(db_path):
    scan(db_path, [f("DCIM/IMG_1.jpg", 700), f("Documents/a.pdf", 300)])
    ov = inv.overview(db_path)
    assert ov["category_totals"] == {"photos": 700, "documents": 300}
    assert ov["volumes"][0]["root_id"] is not None


def test_folder_view_has_breadcrumb_children_and_files(db_path):
    vol = scan(db_path, [f("A/B/big.bin", 900), f("A/small.txt", 5)])
    d = dirs_by_path(db_path, vol)
    out = inv.folder(db_path, d["A"]["id"])
    assert [c["name"] for c in out["breadcrumb"]] == ["(drive root)", "A"]
    assert [c["name"] for c in out["children"]] == ["B"]
    assert [x["name"] for x in out["files"]] == ["small.txt"]


def test_parallel_scans_never_share_folder_ids(db_path):
    h = inv.upsert_host(db_path, "h", "1", "linux")
    v1, v2 = inv.upsert_volume(db_path, h, "u1", "/a"), inv.upsert_volume(db_path, h, "u2", "/b")
    w1 = inv.ScanWriter(db_path, v1, inv.start_scan(db_path, v1))
    w2 = inv.ScanWriter(db_path, v2, inv.start_scan(db_path, v2))
    for i in range(3):
        w1.add(*f(f"x{i}/a.txt"))
        w2.add(*f(f"y{i}/a.txt"))
    w1.finish()
    w2.finish()
    with inv.connect(db_path) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM inv_dirs")]
    assert len(ids) == len(set(ids)) == 8


@pytest.mark.parametrize("path,expected", [
    ("leonardoAI Work/render_01.png", "side_hustle"),
    ("Jarvis Generated/a.png", "side_hustle"),
    ("CleanedUp/flags/usa.png", "unsorted"),      # images, but not camera photos: don't guess
    ("home/.ollama/models/blob", "software"),
])
def test_suggestions_after_first_real_drives(db_path, path, expected):
    vol = scan(db_path, [f(path, 50_000)])
    folder = path.rsplit("/", 1)[0]
    assert dirs_by_path(db_path, vol)[folder]["suggested"] == expected


def test_reclassify_reproduces_scan_time_suggestions_and_keeps_tags(db_path):
    vol = scan(db_path, [f("DCIM/IMG_1.jpg"), f("Etsy/x.svg"), f("misc/a.pdf")])
    before = {p: r["suggested"] for p, r in dirs_by_path(db_path, vol).items()}
    inv.set_tag(db_path, dirs_by_path(db_path, vol)["misc"]["id"], "side_hustle")

    inv.reclassify(db_path)

    after = dirs_by_path(db_path, vol)
    assert {p: r["suggested"] for p, r in after.items()} == before
    assert after["misc"]["category"] == "side_hustle"
