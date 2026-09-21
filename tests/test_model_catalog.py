"""The model catalogue: who fronts which product category.

Jack's rule is one face per category, held steady -- "boy tshirts is the same guy, girls
the same girl". Two behaviours carry that: shots of the same described person must collapse
into one persona, and assigning a category must displace whoever held it rather than
quietly leaving two faces competing for the same products.
"""
import os

import pytest

from assistant.core import model_catalog as mc


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "models.db")
    mc.init_model_catalog(path)
    return path


class TestParsingLeonardoNames:
    def test_the_generator_is_stripped_and_kept(self):
        r = mc.parse_file_name("Phoenix_10_A_woman_in_her_late_30s_dirty_blonde_hair_2.jpg")
        assert r["generator"] == "Phoenix_10"
        assert not r["description"].startswith("Phoenix")

    def test_variation_indexes_collapse_into_one_person(self):
        """The whole grouping depends on this. Without it the catalogue holds 338
        strangers instead of 275 people with several shots each."""
        keys = {mc.parse_file_name(f"Lucid_Origin_Gen_X_man_late_40s_{i}.jpg")["key"]
                for i in range(4)}
        assert len(keys) == 1

    def test_an_is_not_mistaken_for_a(self):
        """Leftmost alternation matched the 'a' of 'An' and left a stray 'n', so every
        androgynous persona was described as 'n androgynous young person'. That reads as
        bad data rather than as a code fault, which is what makes it worth a test."""
        r = mc.parse_file_name("Phoenix_10_An_androgynous_young_person_with_short_hair_0.jpg")
        assert r["description"].lower().startswith("androgynous")
        assert r["gender"] == "androgynous"

    @pytest.mark.parametrize("name,gender", [
        ("Lucid_Origin_White_male_in_his_early_20s_messy_0.jpg", "male"),
        ("Phoenix_10_A_woman_in_her_late_30s_blonde_0.jpg", "female"),
        ("Lucid_Origin_a_cinematic_photo_of_Young_girl_smiling_0.jpg", "female"),
    ])
    def test_gender_is_read_when_stated(self, name, gender):
        assert mc.parse_file_name(name)["gender"] == gender

    @pytest.mark.parametrize("name,age", [
        ("Phoenix_10_A_woman_in_her_late_30s_blonde_0.jpg", "late 30s"),
        ("Lucid_Origin_White_male_in_his_early_20s_messy_0.jpg", "early 20s"),
        ("Lucid_Origin_Young_woman_age_18_natural_red_hair_0.jpg", "age 18"),
    ])
    def test_age_keeps_its_qualifier(self, name, age):
        """'late 20s' must not degrade to '20s' -- the qualifier is most of the meaning."""
        assert mc.parse_file_name(name)["age_band"] == age

    def test_an_unreadable_name_still_yields_a_key(self):
        """Every field may be None, but the key groups the shots and cannot be."""
        r = mc.parse_file_name("IMG_4821.jpg")
        assert r["key"] and r["gender"] is None and r["age_band"] is None

    def test_a_guess_is_never_invented(self):
        """The folder holds product shots as well as people. A wrong guess about who
        someone is would propagate into which products they front."""
        r = mc.parse_file_name("Lucid_Origin_a_cinematic_photo_of_Cuffed_Beanie_0.jpg")
        assert r["gender"] is None


class TestImport:
    def _folder(self, tmp_path, names):
        from PIL import Image

        folder = tmp_path / "models"
        folder.mkdir()
        for n in names:
            Image.new("RGB", (64, 48), (200, 200, 200)).save(folder / n)
        return str(folder)

    def test_shots_of_one_person_become_one_persona(self, db, tmp_path):
        folder = self._folder(tmp_path, [
            "Phoenix_10_A_woman_in_her_late_30s_blonde_hair_0.jpg",
            "Phoenix_10_A_woman_in_her_late_30s_blonde_hair_1.jpg",
            "Phoenix_10_A_woman_in_her_late_30s_blonde_hair_2.jpg",
            "Lucid_Origin_Gen_X_man_in_his_late_40s_salt_pepper_0.jpg"])
        result = mc.import_directory(db, folder)
        assert result["images_added"] == 4 and result["personas_added"] == 2
        shots = sorted(p["shots"] for p in mc.list_personas(db))
        assert shots == [1, 3]

    def test_re_importing_adds_nothing_and_preserves_assignments(self, db, tmp_path):
        """The category assignment is the expensive decision. An import that reset it
        would be worse than no import at all."""
        folder = self._folder(tmp_path, ["Phoenix_10_A_woman_late_30s_blonde_0.jpg"])
        mc.import_directory(db, folder)
        pid = mc.list_personas(db)[0]["id"]
        mc.assign_category(db, pid, "womens-tees")

        again = mc.import_directory(db, folder)
        assert again["images_added"] == 0 and again["already_known"] == 1
        assert mc.category_face(db, "womens-tees")["id"] == pid

    def test_new_files_are_picked_up_on_a_later_run(self, db, tmp_path):
        from PIL import Image

        folder = self._folder(tmp_path, ["Phoenix_10_A_woman_late_30s_0.jpg"])
        mc.import_directory(db, folder)
        Image.new("RGB", (64, 48)).save(os.path.join(folder, "Phoenix_10_A_man_early_20s_0.jpg"))
        assert mc.import_directory(db, folder)["images_added"] == 1

    def test_dimensions_are_recorded(self, db, tmp_path):
        folder = self._folder(tmp_path, ["Phoenix_10_A_woman_late_30s_0.jpg"])
        mc.import_directory(db, folder)
        image = mc.persona_images(db, mc.list_personas(db)[0]["id"])[0]
        assert (image["width"], image["height"]) == (64, 48)

    def test_a_missing_folder_says_so(self, db):
        with pytest.raises(FileNotFoundError):
            mc.import_directory(db, os.path.join("definitely", "not", "here"))


class TestOneFacePerCategory:
    @pytest.fixture
    def two(self, db, tmp_path):
        from PIL import Image

        folder = tmp_path / "m"
        folder.mkdir()
        for n in ["Phoenix_10_A_woman_late_30s_0.jpg", "Phoenix_10_A_man_early_20s_0.jpg"]:
            Image.new("RGB", (32, 32)).save(folder / n)
        mc.import_directory(db, str(folder))
        return [p["id"] for p in mc.list_personas(db)]

    def test_assigning_a_category_makes_that_face_the_category(self, db, two):
        mc.assign_category(db, two[0], "Mens-Tees")
        face = mc.category_face(db, "mens-tees")
        assert face["id"] == two[0] and face["status"] == "active"
        assert face["images"], "a mockup job needs the shots, not just the row"

    def test_a_second_assignment_displaces_the_first(self, db, two):
        """One face per category is the entire rule. Without displacement a category ends
        up with two faces that alternate between products -- the exact inconsistency the
        catalogue exists to prevent."""
        mc.assign_category(db, two[0], "mugs")
        result = mc.assign_category(db, two[1], "mugs")
        assert [r["id"] for r in result["replaced"]] == [two[0]]
        assert mc.category_face(db, "mugs")["id"] == two[1]
        assert len(mc.list_personas(db, assigned_category="mugs")) == 1

    def test_a_displaced_persona_returns_to_the_pool(self, db, two):
        mc.assign_category(db, two[0], "mugs")
        mc.assign_category(db, two[1], "mugs")
        pool = {p["id"]: p for p in mc.list_personas(db)}
        assert pool[two[0]]["assigned_category"] is None
        assert pool[two[0]]["status"] == "candidate", "still available, not retired"

    def test_one_persona_fronts_only_its_latest_category(self, db, two):
        mc.assign_category(db, two[0], "mugs")
        mc.assign_category(db, two[0], "hats")
        assert mc.category_face(db, "mugs") is None
        assert mc.category_face(db, "hats")["id"] == two[0]

    def test_an_unassigned_category_has_no_face(self, db, two):
        assert mc.category_face(db, "nothing-here") is None

    def test_a_blank_category_is_refused(self, db, two):
        with pytest.raises(ValueError):
            mc.assign_category(db, two[0], "   ")

    def test_the_summary_counts_what_is_assigned(self, db, two):
        mc.assign_category(db, two[0], "mugs")
        summary = mc.catalog_summary(db)
        assert summary["personas"] == 2 and summary["images"] == 2
        assert summary["categories"] == {"mugs": 1}
