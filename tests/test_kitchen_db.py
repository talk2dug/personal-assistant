"""kitchen_db.py's inventory primitives -- upsert_inventory_item is the one write path
every source (manual, receipt, Kroger sync, cook-deduction, photo recount) funnels
through, so its clamping/shortfall/logging behavior matters more than any one caller.
Also covers migrate_pantry_to_inventory, the one-time move off personal_db's old
have/low/out enum.
"""
import json

import pytest

from assistant.core import db, kitchen_db, personal_db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    kitchen_db.init_kitchen_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


def test_upsert_creates_a_new_item_with_a_delta_from_zero(db_path, owner_id):
    result = kitchen_db.upsert_inventory_item(
        db_path, owner_id, "eggs", quantity_delta=12, unit="count", reason="purchase_manual")

    assert result["item"] == "eggs"
    assert result["quantity"] == 12
    assert result["status"] == "have"
    assert result["shortfall"] is None


def test_delta_accumulates_on_the_same_normalized_item(db_path, owner_id):
    kitchen_db.upsert_inventory_item(db_path, owner_id, "Milk", quantity_delta=2, unit="gal", reason="purchase_manual")
    result = kitchen_db.upsert_inventory_item(db_path, owner_id, "milk!", quantity_delta=-1.5, reason="cook_deduction")

    assert result["quantity"] == pytest.approx(0.5)
    assert len(kitchen_db.list_inventory(db_path, owner_id)) == 1


def test_quantity_set_overrides_rather_than_accumulates(db_path, owner_id):
    kitchen_db.upsert_inventory_item(db_path, owner_id, "rice", quantity_delta=10, reason="purchase_manual")
    result = kitchen_db.upsert_inventory_item(db_path, owner_id, "rice", quantity_set=2.5, reason="photo_recount")

    assert result["quantity"] == 2.5


def test_deduction_past_zero_clamps_and_reports_shortfall(db_path, owner_id):
    kitchen_db.upsert_inventory_item(db_path, owner_id, "flour", quantity_delta=1, reason="purchase_manual")
    result = kitchen_db.upsert_inventory_item(db_path, owner_id, "flour", quantity_delta=-3, reason="cook_deduction")

    assert result["quantity"] == 0
    assert result["shortfall"] == pytest.approx(2)


def test_status_is_derived_from_quantity_and_threshold(db_path, owner_id):
    have = kitchen_db.upsert_inventory_item(db_path, owner_id, "pasta", quantity_set=10, low_threshold=2)
    low = kitchen_db.upsert_inventory_item(db_path, owner_id, "butter", quantity_set=1, low_threshold=2)
    out = kitchen_db.upsert_inventory_item(db_path, owner_id, "eggs", quantity_set=0, low_threshold=2)

    assert have["status"] == "have"
    assert low["status"] == "low"
    assert out["status"] == "out"


def test_list_inventory_filters_by_derived_status(db_path, owner_id):
    kitchen_db.upsert_inventory_item(db_path, owner_id, "pasta", quantity_set=10, low_threshold=2)
    kitchen_db.upsert_inventory_item(db_path, owner_id, "butter", quantity_set=0, low_threshold=2)

    out_only = kitchen_db.list_inventory(db_path, owner_id, status="out")
    assert len(out_only) == 1 and out_only[0]["item"] == "butter"


def test_get_inventory_item_matches_by_normalized_name(db_path, owner_id):
    kitchen_db.upsert_inventory_item(db_path, owner_id, "Ground Beef", quantity_delta=1, unit="lb")

    found = kitchen_db.get_inventory_item(db_path, owner_id, "  ground beef!! ")
    assert found is not None and found["item"] == "Ground Beef"

    assert kitchen_db.get_inventory_item(db_path, owner_id, "kale") is None


def test_delete_inventory_item_removes_it(db_path, owner_id):
    row = kitchen_db.upsert_inventory_item(db_path, owner_id, "kale", quantity_delta=1)
    assert kitchen_db.delete_inventory_item(db_path, owner_id, row["id"]) is True
    assert kitchen_db.get_inventory_item(db_path, owner_id, "kale") is None
    assert kitchen_db.delete_inventory_item(db_path, owner_id, row["id"]) is False


def test_every_write_is_logged(db_path, owner_id):
    kitchen_db.upsert_inventory_item(db_path, owner_id, "eggs", quantity_delta=12, reason="purchase_manual")
    kitchen_db.upsert_inventory_item(db_path, owner_id, "eggs", quantity_delta=-2, reason="cook_deduction")

    log = kitchen_db.inventory_log(db_path, owner_id, item="eggs")
    assert len(log) == 2
    assert log[0]["reason"] == "cook_deduction"  # most recent first
    assert log[1]["reason"] == "purchase_manual"


def test_migrate_pantry_to_inventory_copies_with_placeholder_quantities(db_path, owner_id):
    personal_db.init_personal_db(db_path)

    # Seed pantry_items directly via SQL -- the old update_pantry_status tool (and its
    # personal_db.upsert_pantry_item helper) were retired along with the pantry board.
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO pantry_items (owner_user_id, item, status, updated_at) VALUES (?, ?, ?, datetime('now'))",
            (owner_id, "milk", "low"),
        )
        conn.execute(
            "INSERT INTO pantry_items (owner_user_id, item, status, updated_at) VALUES (?, ?, ?, datetime('now'))",
            (owner_id, "eggs", "out"),
        )
        conn.commit()

    migrated = kitchen_db.migrate_pantry_to_inventory(db_path, owner_id)

    assert set(migrated) == {"milk", "eggs"}
    milk = kitchen_db.get_inventory_item(db_path, owner_id, "milk")
    eggs = kitchen_db.get_inventory_item(db_path, owner_id, "eggs")
    assert milk["quantity"] == 2.0
    assert eggs["quantity"] == 0.0
    assert personal_db.list_pantry(db_path, owner_id) == []


def test_migrate_pantry_to_inventory_is_idempotent(db_path, owner_id):
    personal_db.init_personal_db(db_path)
    assert kitchen_db.migrate_pantry_to_inventory(db_path, owner_id) == []
    assert kitchen_db.migrate_pantry_to_inventory(db_path, owner_id) == []


# --- sync_kroger_orders ----------------------------------------------------------
# Real shapes confirmed via a live call against the real running service before this was
# written: an account with no orders returns {"success": True, "orders": [], "showing": 0,
# "summary": {...}}. The non-empty order shape below is reconstructed from kroger-mcp's
# own source (mark_order_placed's order_record / view_order_history's response), since
# the real account had nothing to show live -- see sync_kroger_orders' own docstring for
# why that shape (a raw product_id, no name) is exactly the limitation this function
# works around with its own get_product_details lookup.

class FakeKrogerMCPClient:
    def __init__(self, orders=None, product_details=None, order_history_error=None):
        self._orders = orders or []
        self._product_details = product_details or {}
        self._order_history_error = order_history_error
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "view_order_history":
            if self._order_history_error:
                return {"is_error": True, "content": [self._order_history_error]}
            payload = {
                "success": True, "orders": self._orders, "showing": len(self._orders),
                "summary": {"total_orders": len(self._orders)},
            }
            return {"is_error": False, "content": [json.dumps(payload)]}
        if name == "get_product_details":
            product_id = arguments["product_id"]
            details = self._product_details.get(product_id)
            if details is None:
                return {"is_error": False, "content": [json.dumps({"success": False, "error": "not found"})]}
            return {"is_error": False, "content": [json.dumps({"success": True, **details})]}
        raise ValueError(f"unexpected tool: {name}")


def test_sync_with_no_orders_is_a_clean_no_op(db_path, owner_id):
    client = FakeKrogerMCPClient(orders=[])

    result = kitchen_db.sync_kroger_orders(client, db_path, owner_id)

    assert result == {"ok": True, "synced_orders": 0, "items_updated": []}
    assert kitchen_db.list_inventory(db_path, owner_id) == []


def test_sync_resolves_product_names_and_updates_inventory(db_path, owner_id):
    client = FakeKrogerMCPClient(
        orders=[{
            "items": [
                {"product_id": "0001111041700", "quantity": 2, "modality": "PICKUP"},
                {"product_id": "0001111060903", "quantity": 1, "modality": "PICKUP"},
            ],
            "placed_at": "2026-09-07T12:00:00", "item_count": 2, "total_quantity": 3, "notes": None,
        }],
        product_details={
            "0001111041700": {"description": "2% Milk, 1 Gallon", "brand": "Kroger"},
            "0001111060903": {"description": "Large Eggs, Dozen", "brand": "Kroger"},
        },
    )

    result = kitchen_db.sync_kroger_orders(client, db_path, owner_id)

    assert result["ok"] is True
    assert result["synced_orders"] == 1
    milk = kitchen_db.get_inventory_item(db_path, owner_id, "2% Milk, 1 Gallon")
    eggs = kitchen_db.get_inventory_item(db_path, owner_id, "Large Eggs, Dozen")
    assert milk["quantity"] == 2
    assert eggs["quantity"] == 1
    log = kitchen_db.inventory_log(db_path, owner_id)
    assert all(entry["reason"] == "purchase_kroger" for entry in log)


def test_sync_falls_back_to_a_placeholder_name_when_lookup_fails(db_path, owner_id):
    client = FakeKrogerMCPClient(orders=[{
        "items": [{"product_id": "9999999999999", "quantity": 1, "modality": "PICKUP"}],
        "placed_at": "2026-09-07T12:00:00", "item_count": 1, "total_quantity": 1, "notes": None,
    }])

    result = kitchen_db.sync_kroger_orders(client, db_path, owner_id)

    assert result["ok"] is True
    item = kitchen_db.get_inventory_item(db_path, owner_id, "Kroger item 9999999999999")
    assert item is not None and item["quantity"] == 1


def test_sync_does_not_double_count_an_order_already_synced(db_path, owner_id):
    client = FakeKrogerMCPClient(
        orders=[{
            "items": [{"product_id": "0001111041700", "quantity": 2, "modality": "PICKUP"}],
            "placed_at": "2026-09-07T12:00:00", "item_count": 1, "total_quantity": 2, "notes": None,
        }],
        product_details={"0001111041700": {"description": "2% Milk, 1 Gallon"}},
    )

    first = kitchen_db.sync_kroger_orders(client, db_path, owner_id)
    second = kitchen_db.sync_kroger_orders(client, db_path, owner_id)

    assert first["synced_orders"] == 1
    assert second["synced_orders"] == 0
    milk = kitchen_db.get_inventory_item(db_path, owner_id, "2% Milk, 1 Gallon")
    assert milk["quantity"] == 2  # not 4 -- the repeat call didn't re-apply the delta


def test_sync_only_folds_in_orders_newer_than_what_was_already_synced(db_path, owner_id):
    client = FakeKrogerMCPClient(
        orders=[{
            "items": [{"product_id": "0001111041700", "quantity": 2, "modality": "PICKUP"}],
            "placed_at": "2026-09-07T12:00:00", "item_count": 1, "total_quantity": 2, "notes": None,
        }],
        product_details={"0001111041700": {"description": "2% Milk, 1 Gallon"}},
    )
    kitchen_db.sync_kroger_orders(client, db_path, owner_id)

    # A second, genuinely new order (different placed_at) shows up on the next poll --
    # the old one must not be re-applied alongside it.
    client._orders.append({
        "items": [{"product_id": "0001111041700", "quantity": 1, "modality": "PICKUP"}],
        "placed_at": "2026-09-07T13:00:00", "item_count": 1, "total_quantity": 1, "notes": None,
    })

    result = kitchen_db.sync_kroger_orders(client, db_path, owner_id)

    assert result["synced_orders"] == 1
    milk = kitchen_db.get_inventory_item(db_path, owner_id, "2% Milk, 1 Gallon")
    assert milk["quantity"] == 3  # 2 from the first sync + 1 from the newly-placed order


def test_sync_reports_a_view_order_history_failure_without_raising(db_path, owner_id):
    client = FakeKrogerMCPClient(order_history_error="Authentication failed")

    result = kitchen_db.sync_kroger_orders(client, db_path, owner_id)

    assert result["ok"] is False
    assert "Authentication failed" in result["error"]
