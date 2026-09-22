"""The artwork catalogue: designs he sends in to make things from later.

His words for what this is: "These mostly will be for me to make things from later...
I would select something from the catalog and make something from it then have the
system create the marketing pipeline and sell it for me."

So the property that matters is that a design can be FOUND and PICKED afterwards. Before
this, artwork he emailed was written to a folder and that was the end of it -- a folder
cannot be asked what is in it, what he called something, or what has been used.
"""
import os

import pytest
from PIL import Image

from assistant.core import design_assets


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "assets.db")
    design_assets.init_design_assets(p)
    return p


@pytest.fixture
def art(tmp_path):
    f = tmp_path / "design.jpg"
    Image.new("RGB", (1200, 800), "white").save(str(f))
    return str(f)


class TestCataloguing:
    def test_a_design_can_be_found_again(self, path, art):
        design_assets.add(path, 1, art, title="Skull design")
        found = design_assets.catalogue(path, 1)
        assert len(found) == 1 and found[0]["title"] == "Skull design"
        assert found[0]["path"] == art

    def test_his_own_words_become_the_title(self, path, art):
        """The filename is IMG_1018.JPG. What he called it is the useful name."""
        design_assets.add(path, 1, art, title="Art work - skull for tees")
        assert design_assets.catalogue(path, 1)[0]["title"] == "Art work - skull for tees"

    def test_with_no_title_it_falls_back_to_the_filename(self, path, art):
        design_assets.add(path, 1, art, title=None)
        assert design_assets.catalogue(path, 1)[0]["title"] == os.path.basename(art)

    def test_dimensions_are_recorded_so_he_can_judge_usability(self, path, art):
        """A 1200x800 design and a 200x140 thumbnail are not the same raw material."""
        design_assets.add(path, 1, art)
        a = design_assets.catalogue(path, 1)[0]
        assert (a["width"], a["height"]) == (1200, 800) and a["bytes"] > 0

    def test_an_unreadable_file_is_still_catalogued(self, path, tmp_path):
        """Refusing to record it would lose the very thing this exists to keep."""
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"not an image")
        assert design_assets.add(path, 1, str(broken)) > 0
        a = design_assets.catalogue(path, 1)[0]
        assert a["width"] is None and a["bytes"] == len(b"not an image")

    def test_newest_first_because_that_is_what_he_is_reaching_for(self, path, tmp_path):
        for i in range(3):
            f = tmp_path / f"d{i}.jpg"
            Image.new("RGB", (10, 10)).save(str(f))
            design_assets.add(path, 1, str(f), title=f"design {i}")
        assert [a["title"] for a in design_assets.catalogue(path, 1)] == \
               ["design 2", "design 1", "design 0"]

    def test_another_owners_artwork_is_not_listed(self, path, art):
        design_assets.add(path, 1, art, title="mine")
        assert design_assets.catalogue(path, 99) == []


class TestPickingOne:
    def test_using_a_design_is_counted_not_consumed(self, path, art):
        """A design he has already sold something from is exactly the one he may want
        again -- so 'used' must not mean gone."""
        asset = design_assets.add(path, 1, art)
        design_assets.mark_used(path, 1, asset)
        design_assets.mark_used(path, 1, asset)
        again = design_assets.get(path, 1, asset)
        assert again["times_used"] == 2 and again["status"] == "used"
        assert design_assets.catalogue(path, 1, status="used")[0]["id"] == asset

    def test_a_used_design_drops_out_of_the_available_list(self, path, art):
        asset = design_assets.add(path, 1, art)
        design_assets.mark_used(path, 1, asset)
        assert design_assets.catalogue(path, 1, status="available") == []

    def test_it_can_be_put_back(self, path, art):
        asset = design_assets.add(path, 1, art)
        design_assets.mark_used(path, 1, asset)
        design_assets.set_status(path, 1, asset, "available")
        assert len(design_assets.catalogue(path, 1)) == 1

    def test_a_nonsense_status_is_refused(self, path, art):
        asset = design_assets.add(path, 1, art)
        with pytest.raises(ValueError):
            design_assets.set_status(path, 1, asset, "printed")

    def test_another_owner_cannot_mark_his_artwork_used(self, path, art):
        asset = design_assets.add(path, 1, art)
        assert design_assets.mark_used(path, 99, asset) is False
