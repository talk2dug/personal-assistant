"""Covers core/storage_mover.py against real temp folders standing in for drives: verified
copy then quarantine, duplicates never copied twice, never overwriting, same-drive renames,
whole-tree moves, resuming, and refusing a source that changed since it was hashed."""
import hashlib
import os

import pytest

from assistant.core import drive_inventory as inv
from assistant.core import storage_mover as sm


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def write(root, rel, data: bytes):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def read(root, rel):
    with open(os.path.join(root, *rel.split("/")), "rb") as f:
        return f.read()


def exists(root, rel):
    return os.path.exists(os.path.join(root, *rel.split("/")))


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "inventory.db")
    sm.init_db(db)
    dest, src = str(tmp_path / "D"), str(tmp_path / "F")
    os.makedirs(dest)
    os.makedirs(src)
    h = inv.upsert_host(db, "jarvisbox", "1", "windows")
    vd = inv.upsert_volume(db, h, "D", dest)
    vf = inv.upsert_volume(db, h, "F", src)
    return {"db": db, "dest": dest, "src": src, "vd": vd, "vf": vf}


def unit(env, uid, path, dest_path, kind="files", **kw):
    u = {"id": kw.get("id", 1), "kind": kind, "source_uid": uid, "dest_uid": "D", "source_path": path,
         "dest_path": dest_path, "new_bytes": 0, "same_drive": int(uid == "D")}
    return u


def mover(env, **kw):
    return sm.Mover(env["db"], {"D": sm.LocalSource(env["dest"]), "F": sm.LocalSource(env["src"])},
                    {"D": env["dest"]}, log=lambda *_: None, **kw)


def test_copy_is_verified_then_the_source_is_quarantined_not_deleted(env):
    write(env["src"], "Bundle/SVG/a.svg", b"art")
    s = mover(env).run_unit(unit(env, "F", "Bundle/SVG", "Side Hustle/SVG & Cut Files/Bundle - SVG"))
    assert s["copied"] == 1
    assert read(env["dest"], "Side Hustle/SVG & Cut Files/Bundle - SVG/a.svg") == b"art"
    assert not exists(env["src"], "Bundle/SVG/a.svg")
    assert read(env["src"], f"_to_delete/{sm.today()}/Bundle/SVG/a.svg") == b"art"
    assert not any(f.endswith(".partial") for _, _, fs in os.walk(env["dest"]) for f in fs)


def test_content_already_kept_is_not_copied_again(env):
    write(env["dest"], "Collected Art/x/a.svg", b"same")
    write(env["src"], "Old/a copy.svg", b"same")
    inv.record_hashes(env["db"], "D", [("Collected Art/x/a.svg", 4, None, sha(b"same"))])
    inv.record_hashes(env["db"], "F", [("Old/a copy.svg", 4, None, sha(b"same"))])
    m = mover(env)
    m.run_unit(unit(env, "D", "Collected Art/x", "Side Hustle/SVG & Cut Files/x", id=1))
    s = m.run_unit(unit(env, "F", "Old", "Side Hustle/SVG & Cut Files/Old", id=2))
    assert s["duplicate"] == 1 and s["copied"] == 0
    assert not exists(env["dest"], "Side Hustle/SVG & Cut Files/Old/a copy.svg")
    assert exists(env["src"], f"_to_delete/{sm.today()}/Old/a copy.svg")


def test_never_overwrites_a_different_file_with_the_same_name(env):
    write(env["dest"], "Side Hustle/Images/B/logo.png", b"theirs")
    write(env["src"], "B/logo.png", b"mine")
    mover(env).run_unit(unit(env, "F", "B", "Side Hustle/Images/B"))
    assert read(env["dest"], "Side Hustle/Images/B/logo.png") == b"theirs"
    assert read(env["dest"], "Side Hustle/Images/B/logo (2).png") == b"mine"


def test_same_drive_units_are_renamed_and_empty_source_folders_removed(env):
    write(env["dest"], "Collected Art/Laptop-E/STLs/cat.stl", b"model")
    s = mover(env).run_unit(unit(env, "D", "Collected Art/Laptop-E/STLs", "Side Hustle/3D Models/STLs"))
    assert s["relocated"] == 1
    assert read(env["dest"], "Side Hustle/3D Models/STLs/cat.stl") == b"model"
    assert not exists(env["dest"], "Collected Art")   # emptied, pruned


def test_a_tree_unit_keeps_its_structure_including_noise_folders(env):
    write(env["src"], "arduino/proj/src/main.cpp", b"code")
    write(env["src"], "arduino/proj/.git/HEAD", b"ref")
    mover(env).run_unit(unit(env, "F", "arduino", "Projects/arduino", kind="tree"))
    assert read(env["dest"], "Projects/arduino/proj/src/main.cpp") == b"code"
    assert read(env["dest"], "Projects/arduino/proj/.git/HEAD") == b"ref"


def test_a_source_that_changed_since_hashing_is_refused(env):
    write(env["src"], "B/a.png", b"edited later")
    inv.record_hashes(env["db"], "F", [("B/a.png", 5, None, sha(b"original"))])
    s = mover(env).run_unit(unit(env, "F", "B", "Side Hustle/Images/B"))
    assert s["failed"] == 1
    assert exists(env["src"], "B/a.png")                        # left exactly where it was
    assert not exists(env["dest"], "Side Hustle/Images/B/a.png")


def test_a_rerun_skips_work_already_verified(env):
    write(env["src"], "B/a.png", b"1")
    u = unit(env, "F", "B", "Side Hustle/Images/B")
    mover(env).run_unit(u)
    write(env["src"], "B/a.png", b"new file, same name, arrived later")
    s = mover(env).run_unit(u)
    assert s["copied"] == 0                                     # a.png was already done for this unit


def test_dry_run_touches_nothing(env):
    write(env["src"], "B/a.png", b"1")
    s = mover(env, dry_run=True).run_unit(unit(env, "F", "B", "Side Hustle/Images/B"))
    assert s["copied"] == 1
    assert exists(env["src"], "B/a.png") and not exists(env["dest"], "Side Hustle")


def test_a_tree_unit_skips_folders_carved_out_as_their_own_units(env):
    import json
    write(env["src"], "Laptop/Docs/letter.pdf", b"doc")
    write(env["src"], "Laptop/Etsy/logo.svg", b"art")
    u = unit(env, "F", "Laptop", "Personal/Documents/Laptop", kind="tree")
    u["excludes"] = json.dumps([{"path": "Laptop/Etsy", "kind": "files"}])
    mover(env).run_unit(u)
    assert exists(env["dest"], "Personal/Documents/Laptop/Docs/letter.pdf")
    assert not exists(env["dest"], "Personal/Documents/Laptop/Etsy/logo.svg")
    assert exists(env["src"], "Laptop/Etsy/logo.svg")          # left for its own unit


def test_the_mover_refuses_a_protected_folder_even_from_a_stale_plan(env):
    write(env["dest"], "Obsidian/Jarvis/note.md", b"live")
    m = sm.Mover(env["db"], {"D": sm.LocalSource(env["dest"])}, {"D": env["dest"]}, log=lambda *_: None,
                 protected={(env["dest"], "Obsidian")})
    with pytest.raises(sm.MoveError):
        m.run_unit(unit(env, "D", "Obsidian/Jarvis", "Side Hustle/Other/Jarvis"))
    assert exists(env["dest"], "Obsidian/Jarvis/note.md")


def test_paths_longer_than_windows_max_path_still_move(env):
    deep = "/".join(["Christmas SVG Design Bundle Vol 4 with an extremely long folder name"] * 4)
    rel = f"{deep}/The weather outside is frightful, but the wine is so delightful-01.png"
    p = sm.long_path(os.path.join(env["dest"], *rel.split("/")))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"wine")
    s = mover(env).run_unit(unit(env, "D", deep, "Side Hustle/Images/Vol 4"))
    assert s["relocated"] == 1 and s["failed"] == 0


def test_whole_folder_moves_are_never_deduplicated(env):
    write(env["dest"], "Side Hustle/Images/x/icon.png", b"same icon")
    write(env["src"], "VinylApp/public/icon.png", b"same icon")
    write(env["src"], "VinylApp/assets/icon.png", b"same icon")
    inv.record_hashes(env["db"], "F", [("VinylApp/public/icon.png", 9, None, sha(b"same icon")),
                                       ("VinylApp/assets/icon.png", 9, None, sha(b"same icon"))])
    m = mover(env)
    m.kept["D"] = {sha(b"same icon")}
    s = m.run_unit(unit(env, "F", "VinylApp", "Side Hustle/Software & Code/VinylApp", kind="tree"))
    assert s["duplicate"] == 0 and s["copied"] == 2
    assert exists(env["dest"], "Side Hustle/Software & Code/VinylApp/public/icon.png")
    assert exists(env["dest"], "Side Hustle/Software & Code/VinylApp/assets/icon.png")


def test_a_resumed_run_quarantines_originals_left_behind_by_an_interruption(env):
    write(env["src"], "B/a.png", b"art")
    u = unit(env, "F", "B", "Side Hustle/Images/B")
    m = mover(env)
    m._log(u, "B/a.png", "copied", "Side Hustle/Images/B/a.png", sha(b"art"), 3)   # stopped right here
    write(env["dest"], "Side Hustle/Images/B/a.png", b"art")
    mover(env).run_unit(u)
    assert not exists(env["src"], "B/a.png")
    assert exists(env["src"], f"_to_delete/{sm.today()}/B/a.png")


def test_a_drive_that_vanished_is_an_error_not_an_empty_folder(env, tmp_path):
    gone = sm.LocalSource(str(tmp_path / "unplugged"))
    with pytest.raises(sm.MoveError):
        gone.walk_files("GoPro")
    with pytest.raises(sm.MoveError):
        gone.list_files("GoPro")


def test_copy_only_drives_are_never_written_to(env):
    write(env["src"], "GoPro/100GOPRO/GX01.MP4", b"footage")
    m = sm.Mover(env["db"], {"F": sm.LocalSource(env["src"])}, {"D": env["dest"]}, log=lambda *_: None,
                 copy_only={"F"})
    s = m.run_unit(unit(env, "F", "GoPro", "Personal/Home Video/GoPro", kind="tree"))
    assert s["copied"] == 1
    assert read(env["dest"], "Personal/Home Video/GoPro/100GOPRO/GX01.MP4") == b"footage"
    assert exists(env["src"], "GoPro/100GOPRO/GX01.MP4")            # original untouched
    assert not exists(env["src"], "_to_delete")                    # nothing written to the drive
