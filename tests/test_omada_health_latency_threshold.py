"""Covers the wireless-aware latency bar in omada_health._ping_is_bad/render_snapshot.

Pinned to the real false positive it fixes: touch1/touch2 (Wi-Fi presence/wake-word
terminal Pis) were flagged '<-- problem' in the network feed repeatedly -- 6 consecutive
reviews -- purely for averaging 35-45ms at 0% packet loss, which is ordinary indoor RF
jitter, not a fault. Wired infrastructure keeps the original strict 30ms bar.
"""
from assistant.core import omada_health


def _snap(ping_rows, clients):
    return {"at": "2026-10-08T15:26:00+00:00", "overview": {}, "devices": [], "clients": clients,
            "ping": ping_rows}


def test_wireless_client_at_zero_loss_under_70ms_is_not_flagged():
    clients = [{"name": "touch2", "ip": "192.168.0.136", "wireless": True}]
    ping_rows = [{"name": "touch2", "ip": "192.168.0.136", "loss_pct": 0.0, "avg_ms": 42.3, "max_ms": 110.0}]
    text = omada_health.render_snapshot(_snap(ping_rows, clients))
    assert "<-- problem" not in text
    assert "1 of 1 clean" in text


def test_wireless_client_is_still_flagged_past_the_looser_bar():
    clients = [{"name": "touch2", "ip": "192.168.0.136", "wireless": True}]
    ping_rows = [{"name": "touch2", "ip": "192.168.0.136", "loss_pct": 0.0, "avg_ms": 95.0, "max_ms": 200.0}]
    text = omada_health.render_snapshot(_snap(ping_rows, clients))
    assert "<-- problem" in text


def test_wired_target_still_uses_the_strict_30ms_bar():
    # Not in the clients list at all (e.g. the router/an AP) -- wired bar applies.
    ping_rows = [{"name": "router", "ip": "192.168.0.1", "loss_pct": 0.0, "avg_ms": 42.3, "max_ms": 50.0}]
    text = omada_health.render_snapshot(_snap(ping_rows, []))
    assert "<-- problem" in text


def test_any_packet_loss_is_still_bad_regardless_of_wireless(): 
    clients = [{"name": "touch2", "ip": "192.168.0.136", "wireless": True}]
    ping_rows = [{"name": "touch2", "ip": "192.168.0.136", "loss_pct": 10.0, "avg_ms": 5.0, "max_ms": 9.0}]
    text = omada_health.render_snapshot(_snap(ping_rows, clients))
    assert "<-- problem" in text
