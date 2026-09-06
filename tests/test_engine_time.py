from assistant.core.engine import _local_to_utc_iso, _utc_to_local_iso


def test_local_to_utc_converts_edt_offset():
    # 2026-09-02 is during EDT (UTC-4)
    utc_iso = _local_to_utc_iso("2026-09-02T09:00:00", "America/New_York")
    assert utc_iso.startswith("2026-09-02T13:00:00")


def test_utc_to_local_converts_edt_offset():
    local_iso = _utc_to_local_iso("2026-09-02T13:00:00+00:00", "America/New_York")
    assert local_iso.startswith("2026-09-02T09:00:00")


def test_round_trip_preserves_instant():
    original = "2026-09-02T09:00:00"
    utc = _local_to_utc_iso(original, "America/New_York")
    back = _utc_to_local_iso(utc, "America/New_York")
    assert back.startswith(original)
