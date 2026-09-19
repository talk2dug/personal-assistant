"""Jarvis can actually run the day, not just have a database that could.

Written because the machinery landed first and the tools did not: the schema, the REST API
and the dashboard were all live while the chat surface had none of it, so being told "move
my wake-up to 6:15" left him with nothing to call.

Every tool here is EXECUTED, not just declared. A tool that appears in the list and then
falls through the dispatch chain is worse than one that was never offered, because the
model reports success either way and the change silently never happened.
"""
import pytest

from assistant.core import db as core_db, personal_db
from assistant.core.personal_tools import PERSONAL_TOOLS, PersonalClient

DAY_TOOLS = ("plan_my_day", "list_day_rhythm", "add_day_rhythm", "update_day_rhythm",
             "log_day_rhythm", "pick_task_for_today", "unpick_task_for_today",
             "add_task_detail", "block_task", "unblock_task")


@pytest.fixture
def client(tmp_path):
    path = str(tmp_path / "day.db")
    core_db.init_db(path)
    personal_db.init_personal_db(path)
    core_db.upsert_user(path, "111", "Dug", "owner")
    owner = core_db.get_user_by_chat_id(path, "111")["id"]
    return PersonalClient(path, owner, tz_name="America/New_York")


def _task_id(client, text):
    return next(t["id"] for t in client.call_tool("list_personal_tasks", {})["tasks"]
                if t["text"] == text)


def test_every_day_tool_is_declared_to_the_model():
    declared = {t["function"]["name"] for t in PERSONAL_TOOLS}
    missing = set(DAY_TOOLS) - declared
    assert not missing, f"declared nowhere, so Jarvis cannot call them: {sorted(missing)}"


def test_every_day_tool_actually_dispatches(client):
    """The check that catches a tool declared but never wired: it would fall through the
    whole if-chain, and the model would cheerfully report it had done the thing."""
    for name in DAY_TOOLS:
        result = client.call_tool(name, {
            "rhythm_id": 1, "task_id": 1, "blocked_by_id": 2,
            "name": "x", "kind": "note", "value": "v"})
        assert isinstance(result, dict), f"{name} returned {type(result)}"
        assert "unknown tool" not in str(result).lower(), f"{name} was never dispatched"


class TestManagingTheRoutineByTalking:
    def test_he_can_add_an_anchor(self, client):
        result = client.call_tool("add_day_rhythm", {
            "name": "Leave for work", "kind": "anchor", "category": "work",
            "at_time": "07:40", "days": "0,1,2,3,4", "lead_minutes": 15, "hard": True})
        assert result["ok"] is True
        item = client.call_tool("list_day_rhythm", {})["rhythm"][0]
        assert item["name"] == "Leave for work"
        assert item["at_time"] == "07:40" and item["hard"] == 1 and item["lead_minutes"] == 15

    def test_he_can_add_a_habit(self, client):
        client.call_tool("add_day_rhythm", {"name": "Stationary bike", "kind": "habit",
                                            "category": "health", "target_per_week": 4})
        item = client.call_tool("list_day_rhythm", {})["rhythm"][0]
        assert item["kind"] == "habit" and item["target_per_week"] == 4

    def test_he_can_move_a_time(self, client):
        """The seeded times were inferred from a wake reminder and a commute distance, not
        given by him, so corrections are the expected first interaction."""
        client.call_tool("add_day_rhythm", {"name": "Wake up", "kind": "anchor",
                                            "at_time": "06:45", "category": "wake"})
        rhythm_id = client.call_tool("list_day_rhythm", {})["rhythm"][0]["id"]
        assert client.call_tool("update_day_rhythm",
                                {"rhythm_id": rhythm_id, "at_time": "06:15"})["ok"] is True
        assert client.call_tool("list_day_rhythm", {})["rhythm"][0]["at_time"] == "06:15"

    def test_he_can_change_which_days_it_runs(self, client):
        client.call_tool("add_day_rhythm", {"name": "Leave for work", "kind": "anchor",
                                            "at_time": "07:40", "category": "work"})
        rhythm_id = client.call_tool("list_day_rhythm", {})["rhythm"][0]["id"]
        client.call_tool("update_day_rhythm", {"rhythm_id": rhythm_id, "days": "0,1,2,3"})
        assert client.call_tool("list_day_rhythm", {})["rhythm"][0]["days"] == "0,1,2,3"

    def test_he_can_turn_one_off_without_losing_its_history(self, client):
        client.call_tool("add_day_rhythm", {"name": "Bike", "kind": "habit",
                                            "target_per_week": 4, "category": "health"})
        rhythm_id = client.call_tool("list_day_rhythm", {})["rhythm"][0]["id"]
        client.call_tool("update_day_rhythm", {"rhythm_id": rhythm_id, "enabled": False})
        assert client.call_tool("list_day_rhythm", {})["rhythm"][0]["enabled"] == 0

    def test_an_anchor_with_no_time_is_refused_with_a_reason(self, client):
        result = client.call_tool("add_day_rhythm", {"name": "No time", "kind": "anchor"})
        assert "error" in result and "time" in result["error"].lower()

    def test_a_habit_with_no_target_is_refused_with_a_reason(self, client):
        result = client.call_tool("add_day_rhythm", {"name": "No target", "kind": "habit"})
        assert "error" in result and "target" in result["error"].lower()

    def test_editing_something_that_does_not_exist_says_so(self, client):
        assert "error" in client.call_tool("update_day_rhythm",
                                           {"rhythm_id": 9999, "at_time": "06:00"})

    def test_he_can_say_he_did_it(self, client):
        client.call_tool("add_day_rhythm", {"name": "Bike", "kind": "habit",
                                            "target_per_week": 4, "category": "health"})
        rhythm_id = client.call_tool("list_day_rhythm", {})["rhythm"][0]["id"]
        assert client.call_tool("log_day_rhythm", {"rhythm_id": rhythm_id})["ok"] is True
        habit = client.call_tool("plan_my_day", {})["habits"][0]
        assert habit["this_week"] == 1 and habit["today_state"] == "done"

    def test_a_skip_is_recorded_as_an_answer_with_its_reason(self, client):
        client.call_tool("add_day_rhythm", {"name": "Bike", "kind": "habit",
                                            "target_per_week": 4, "category": "health"})
        rhythm_id = client.call_tool("list_day_rhythm", {})["rhythm"][0]["id"]
        client.call_tool("log_day_rhythm", {"rhythm_id": rhythm_id, "state": "skipped",
                                            "note": "away that night"})
        habit = client.call_tool("plan_my_day", {})["habits"][0]
        assert habit["today_state"] == "skipped"
        assert habit["this_week"] == 0, "a skip is not progress"

    def test_logging_someone_elses_rhythm_is_refused(self, client):
        """rhythm_log keys on rhythm_id alone, so ownership has to be checked at this
        layer or one signed-in user could write to another's record."""
        theirs = personal_db.add_rhythm(client.db_path, 999, "Theirs", "habit",
                                        target_per_week=1)
        assert "error" in client.call_tool("log_day_rhythm", {"rhythm_id": theirs})


class TestPlanningTheDayByTalking:
    def test_plan_my_day_returns_the_whole_picture_in_one_call(self, client):
        client.call_tool("add_day_rhythm", {"name": "Feed Ghost", "kind": "anchor",
                                            "at_time": "07:00", "category": "care"})
        client.call_tool("create_personal_task", {"text": "Find a vet for Ghost"})
        plan = client.call_tool("plan_my_day", {})
        assert [a["name"] for a in plan["anchors"]] == ["Feed Ghost"]
        assert [t["text"] for t in plan["pick_from"]] == ["Find a vet for Ghost"]
        assert plan["picked"] == [] and plan["blocked"] == []

    def test_it_shows_his_life_not_the_work_of_building_jarvis(self, client):
        client.call_tool("create_personal_task", {"text": "Find a vet"})
        client.call_tool("create_personal_task", {"text": "Wake-word arbitration",
                                                  "track": "project"})
        assert [t["text"] for t in client.call_tool("plan_my_day", {})["pick_from"]] \
            == ["Find a vet"]

    def test_the_build_side_is_reachable_when_he_asks_for_it(self, client):
        client.call_tool("create_personal_task", {"text": "Wake-word arbitration",
                                                  "track": "project"})
        plan = client.call_tool("plan_my_day", {"track": "project"})
        assert [t["text"] for t in plan["pick_from"]] == ["Wake-word arbitration"]

    def test_a_new_task_is_personal_unless_told_otherwise(self, client):
        client.call_tool("create_personal_task", {"text": "Find a vet"})
        assert len(client.call_tool("plan_my_day", {})["pick_from"]) == 1

    def test_he_can_choose_and_unchoose_a_task(self, client):
        client.call_tool("create_personal_task", {"text": "Find a vet"})
        task_id = _task_id(client, "Find a vet")
        client.call_tool("pick_task_for_today", {"task_id": task_id})
        plan = client.call_tool("plan_my_day", {})
        assert [t["id"] for t in plan["picked"]] == [task_id]
        assert plan["pick_from"] == [], "already chosen, so not re-offered"
        client.call_tool("unpick_task_for_today", {"task_id": task_id})
        assert client.call_tool("plan_my_day", {})["picked"] == []


class TestRecordingWhatATaskNeeds:
    def test_he_can_attach_a_phone_number_in_passing(self, client):
        client.call_tool("create_personal_task", {"text": "Find a vet"})
        task_id = _task_id(client, "Find a vet")
        assert client.call_tool("add_task_detail", {
            "task_id": task_id, "kind": "phone", "value": "(512) 555-0142",
            "label": "Old vet"})["ok"] is True
        detail = client.call_tool("plan_my_day", {})["pick_from"][0]["details"][0]
        assert detail["value"] == "(512) 555-0142", "kept exactly as he said it"
        assert detail["label"] == "Old vet"

    def test_details_reach_the_day_plan_so_he_has_them_when_he_picks_it(self, client):
        client.call_tool("create_personal_task", {"text": "Find a vet"})
        task_id = _task_id(client, "Find a vet")
        client.call_tool("add_task_detail", {"task_id": task_id, "kind": "address",
                                             "value": "123 W Broad St, Richmond VA"})
        client.call_tool("pick_task_for_today", {"task_id": task_id})
        picked = client.call_tool("plan_my_day", {})["picked"][0]
        assert picked["details"][0]["value"] == "123 W Broad St, Richmond VA"

    def test_a_bad_detail_kind_is_refused_with_a_reason(self, client):
        client.call_tool("create_personal_task", {"text": "Thing"})
        task_id = _task_id(client, "Thing")
        result = client.call_tool("add_task_detail", {"task_id": task_id, "kind": "sms",
                                                      "value": "x"})
        assert "error" in result


class TestRecordingTheOrderThingsHaveToHappenIn:
    def _two_tasks(self, client):
        client.call_tool("create_personal_task", {"text": "Find a vet"})
        client.call_tool("create_personal_task", {"text": "Find boarding"})
        return _task_id(client, "Find a vet"), _task_id(client, "Find boarding")

    def test_he_can_say_one_thing_waits_on_another(self, client):
        vet, boarding = self._two_tasks(client)
        assert client.call_tool("block_task", {"task_id": boarding,
                                               "blocked_by_id": vet})["ok"] is True
        plan = client.call_tool("plan_my_day", {})
        assert [t["text"] for t in plan["pick_from"]] == ["Find a vet"]
        assert plan["blocked"][0]["text"] == "Find boarding"
        assert plan["blocked"][0]["waiting_on"][0]["text"] == "Find a vet"

    def test_a_circular_dependency_is_refused_with_a_reason(self, client):
        vet, boarding = self._two_tasks(client)
        client.call_tool("block_task", {"task_id": boarding, "blocked_by_id": vet})
        result = client.call_tool("block_task", {"task_id": vet, "blocked_by_id": boarding})
        assert "error" in result

    def test_blocking_a_task_on_itself_is_refused(self, client):
        vet, _ = self._two_tasks(client)
        assert "error" in client.call_tool("block_task", {"task_id": vet,
                                                          "blocked_by_id": vet})

    def test_unblocking_returns_it_to_the_shortlist(self, client):
        vet, boarding = self._two_tasks(client)
        client.call_tool("block_task", {"task_id": boarding, "blocked_by_id": vet})
        client.call_tool("unblock_task", {"task_id": boarding, "blocked_by_id": vet})
        assert len(client.call_tool("plan_my_day", {})["pick_from"]) == 2

    def test_finishing_the_blocker_frees_it_with_no_further_call(self, client):
        vet, boarding = self._two_tasks(client)
        client.call_tool("block_task", {"task_id": boarding, "blocked_by_id": vet})
        client.call_tool("update_personal_task", {"task_id": vet, "status": "done"})
        plan = client.call_tool("plan_my_day", {})
        assert [t["text"] for t in plan["pick_from"]] == ["Find boarding"]
        assert plan["blocked"] == []
