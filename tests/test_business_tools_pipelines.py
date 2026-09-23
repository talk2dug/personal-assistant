"""list_pipelines/get_pipeline on BusinessClient (assistant/core/business_tools.py).

The gap these close: assistant/core/pipelines.py already assembles the trend-to-revenue
chain (trend lead -> concept -> art -> listing -> social, with pending approvals attached)
for the Pipelines web page, but nothing let Jarvis's own chat agent -- or anything calling
through /api/tools/call -- read that assembled state. The flat list_trend_leads/
list_product_concepts/etc. tools show each table on its own, never the chain.
"""
import pytest

from assistant.core import business_db, business_tools, db, pipelines


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "biz.db")
    db.init_db(path)
    business_db.init_business_db(path)
    pipelines.init_pipelines(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


@pytest.fixture
def client(db_path, owner):
    return business_tools.BusinessClient(db_path, owner_user_id=owner)


def _concept(db_path, owner, name, product_type="laser", trend_lead_id=None):
    concept_id, _created = business_db.create_product_concept(
        db_path, owner, name=name, product_type=product_type,
        description=f"{name} description", trend_lead_id=trend_lead_id, source="trend")
    return concept_id


class TestListPipelines:
    def test_returns_a_summary_per_concept(self, client, db_path, owner):
        _concept(db_path, owner, "Laser tumbler", "laser")
        result = client.call_tool("list_pipelines", {})
        assert [p["name"] for p in result["pipelines"]] == ["Laser tumbler"]
        assert result["pipelines"][0]["stage"] == "concept"

    def test_filters_by_market(self, client, db_path, owner):
        _concept(db_path, owner, "Laser tumbler", "laser")
        _concept(db_path, owner, "Tee", "apparel")
        result = client.call_tool("list_pipelines", {"market": "automated"})
        assert [p["name"] for p in result["pipelines"]] == ["Tee"]

    def test_stage_reflects_what_has_actually_been_made(self, client, db_path, owner):
        concept_id = _concept(db_path, owner, "Laser tumbler", "laser")
        business_db.create_art_brief(db_path, owner, "Brief", concept_id=concept_id,
                                     style_direction="style", image_prompt="prompt")
        result = client.call_tool("list_pipelines", {})
        assert result["pipelines"][0]["stage"] == "art"


class TestGetPipeline:
    def test_the_whole_chain_comes_back(self, client, db_path, owner):
        business_db.upsert_trend_lead(
            db_path, owner, "engraved tumblers", source="trend_scout", score=80,
            product_idea="engraved tumblers", reasoning="trending on social")
        lead_id = business_db.list_trend_leads(db_path, owner)[0]["id"]
        concept_id = _concept(db_path, owner, "Laser tumbler", "laser", trend_lead_id=lead_id)
        business_db.create_art_brief(db_path, owner, "Brief", concept_id=concept_id,
                                     style_direction="style", image_prompt="prompt")

        result = client.call_tool("get_pipeline", {"concept_id": concept_id})
        assert result["concept"]["name"] == "Laser tumbler"
        assert result["lead"]["topic"] == "engraved tumblers"
        stage_names = [s["stage"] for s in result["stages"]]
        assert stage_names == ["idea", "concept", "art", "listing", "social"]
        art_stage = next(s for s in result["stages"] if s["stage"] == "art")
        assert len(art_stage["items"]) == 1

    def test_an_unknown_concept_is_an_error_not_a_crash(self, client):
        result = client.call_tool("get_pipeline", {"concept_id": 999999})
        assert "error" in result

    def test_another_owners_pipeline_is_not_readable(self, db_path):
        other_owner_concept = _concept(db_path, 99, "Someone else's product")
        client = business_tools.BusinessClient(db_path, owner_user_id=1)
        result = client.call_tool("get_pipeline", {"concept_id": other_owner_concept})
        assert "error" in result


class TestToolIsRegistered:
    def test_both_tools_are_in_the_schema(self):
        names = {t["function"]["name"] for t in business_tools.BUSINESS_TOOLS}
        assert {"list_pipelines", "get_pipeline"} <= names
