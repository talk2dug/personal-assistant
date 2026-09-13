"""ui_content.py: the one-shot staging table behind show_content, same shape as
vision.py's pending_camera_views -- see that module's test_pending_camera_view_* tests
for the precedent this mirrors."""
from assistant.core import ui_content


def test_pop_pending_content_is_one_shot(tmp_path):
    db_path = str(tmp_path / "test.db")
    ui_content.init_ui_content_db(db_path)
    ui_content.set_pending_content(db_path, 1, "text", {"title": "Draft", "body": "Hello"})

    first = ui_content.pop_pending_content(db_path, 1)
    assert first == {"kind": "text", "title": "Draft", "body": "Hello"}
    assert ui_content.pop_pending_content(db_path, 1) is None


def test_pending_content_is_per_user(tmp_path):
    db_path = str(tmp_path / "test.db")
    ui_content.init_ui_content_db(db_path)
    ui_content.set_pending_content(db_path, 1, "text", {"title": "For user 1", "body": "x"})

    assert ui_content.pop_pending_content(db_path, 2) is None
    assert ui_content.pop_pending_content(db_path, 1) is not None


def test_setting_again_overwrites_rather_than_stacking(tmp_path):
    db_path = str(tmp_path / "test.db")
    ui_content.init_ui_content_db(db_path)
    ui_content.set_pending_content(db_path, 1, "text", {"title": "First", "body": "x"})
    ui_content.set_pending_content(db_path, 1, "review_item", {"review_item_id": 42})

    result = ui_content.pop_pending_content(db_path, 1)
    assert result == {"kind": "review_item", "review_item_id": 42}
    assert ui_content.pop_pending_content(db_path, 1) is None


def test_init_is_idempotent(tmp_path):
    db_path = str(tmp_path / "test.db")
    ui_content.init_ui_content_db(db_path)
    ui_content.init_ui_content_db(db_path)  # must not raise
    ui_content.set_pending_content(db_path, 1, "text", {"title": "t", "body": "b"})
    assert ui_content.pop_pending_content(db_path, 1) is not None
