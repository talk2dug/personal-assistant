#!/usr/bin/env python3
"""Jarvis cellular agent — runs on the Pi that holds the LTE modem (jarvisaudio2).

Deliberately thin, exactly like jarvis_device.py: it owns the radio and nothing else.
No language model, no decision about who may talk to Jarvis, no idea what any message
means. It moves texts between ModemManager and the server, and that is all.

    inbound:   modem  ->  POST /api/cellular/inbound   ->  delete from the modem
    outbound:  GET /api/cellular/outbox  ->  modem  ->  POST /api/cellular/sent/<id>

The Pi polls; the server never calls in. That way this box can sit on wifi, on its own
cellular data, or be moved to another network entirely without anything on the server
needing to know where it went — and there is no inbound port to open on the one device
whose whole purpose is to still be reachable when everything else is not.

The allow-list decision lives on the SERVER, not here. This is a radio on a shelf; it
must not be the thing deciding who gets to spend the owner's money.

Why SMS rather than the data bearer: SMS is not the internet. It needs no APN, no
bearer, no router, and about five watts. When the WAN is down and the house is on
battery, this is the channel that still works — which is the entire point of it.

Install as a systemd unit (see --install) so it comes back after a reboot.
"""
import argparse
import json
import logging
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

log = logging.getLogger("jarvis-cellular")

POLL_SECONDS = 10
# Keep one HTTP call from wedging the loop forever. An inbound POST runs a full Jarvis
# turn server-side, which can legitimately take minutes on a tool-using answer, so it
# gets a much longer budget than the outbox poll.
INBOUND_TIMEOUT = 600
POLL_TIMEOUT = 30


def mmcli(*args: str, timeout: int = 30) -> str:
    out = subprocess.run(["sudo", "-n", "mmcli", *args], capture_output=True,
                         text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:200])
    return out.stdout


def modem_index() -> str:
    """Find the modem each time rather than caching it: a USB re-enumeration or a
    ModemManager restart renumbers it, and a cached index turns into a dead agent that
    looks alive."""
    out = mmcli("-L")
    match = re.search(r"/Modem/(\d+)", out)
    if not match:
        raise RuntimeError("no modem found")
    return match.group(1)


def _field(block: str, label: str) -> str | None:
    match = re.search(rf"{label}:\s*(.+)", block)
    return match.group(1).strip() if match else None


def list_inbound(idx: str) -> list[dict]:
    """Every received SMS currently in the modem, with its text.

    Messages still in 'receiving' state are skipped: a multipart message whose parts
    have not all arrived has incomplete text, and forwarding half a sentence to the
    assistant is worse than waiting one poll.
    """
    messages = []
    for path in re.findall(r"(/org/freedesktop/ModemManager1/SMS/\d+)", mmcli("-m", idx,
                                                                              "--messaging-list-sms")):
        try:
            block = mmcli("-s", path)
        except RuntimeError:
            continue
        if "pdu type: deliver" not in block:
            continue                      # ours, not theirs
        if _field(block, "state") != "received":
            continue                      # still assembling
        text = _field(block, "text")
        if not text:
            continue                      # binary carrier push (e.g. com.vzwdmserver)
        messages.append({"path": path, "number": _field(block, "number") or "",
                         "text": text, "timestamp": _field(block, "timestamp")})
    return messages


def send_sms(idx: str, number: str, text: str) -> None:
    payload = json.dumps(text)[1:-1].replace("'", "")   # quote-safe for mmcli's parser
    created = mmcli("-m", idx,
                    f"--messaging-create-sms=text='{payload}',number='{number}'")
    match = re.search(r"(/org/freedesktop/ModemManager1/SMS/\d+)", created)
    if not match:
        raise RuntimeError(f"could not create SMS: {created.strip()[:120]}")
    mmcli("-s", match.group(1), "--send", timeout=120)


def delete_sms(idx: str, path: str) -> None:
    try:
        mmcli("-m", idx, f"--messaging-delete-sms={path}")
    except RuntimeError as e:
        # A class-1 carrier message cannot be deleted through mmcli. Not fatal: the
        # server dedupes on content, so a message that will not die is answered once
        # and then ignored forever after.
        log.debug("could not delete %s: %s", path, e)


class Server:
    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/")
        self.key = key

    def _call(self, path: str, body: dict | None = None, timeout: int = POLL_TIMEOUT):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", "x-device-key": self.key})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    def deliver(self, msg: dict) -> dict:
        return self._call("/api/cellular/inbound", {
            "number": msg["number"], "text": msg["text"],
            "timestamp": msg.get("timestamp")}, timeout=INBOUND_TIMEOUT)

    def outbox(self) -> list[dict]:
        return self._call("/api/cellular/outbox").get("messages", [])

    def confirm(self, message_id: int, ok: bool, detail: str | None = None) -> None:
        self._call(f"/api/cellular/sent/{message_id}", {"ok": ok, "detail": detail})

    def link_wanted(self) -> bool:
        return bool(self._call("/api/cellular/link").get("data_wanted"))


# The APN for the data bearer. Verizon consumer/hotspot.
DATA_APN = "vzwinternet"


def connect_data(idx: str) -> None:
    """Bring the cellular data bearer up and make it this Pi's route to the internet.

    Changing THIS box's default route is safe and is the point: during an outage its
    normal route runs through a router whose WAN is dead. The LAN route is left alone so
    the server can still reach it, and nothing on any other machine is touched -- the
    whole reason the proxy design was chosen over making this a gateway.
    """
    out = mmcli("-m", idx, f"--simple-connect=apn={DATA_APN}", timeout=90)
    log.info("cellular data: %s", out.strip()[:80])

    block = mmcli("-m", idx)
    bearer = re.findall(r"/org/freedesktop/ModemManager1/Bearer/\d+", block)
    if not bearer:
        raise RuntimeError("connected but no bearer path")
    cfg = mmcli("-b", bearer[-1])
    addr = _field(cfg, "address")
    prefix = _field(cfg, "prefix")
    gw = _field(cfg, "gateway")
    mtu = _field(cfg, "mtu")
    if not (addr and prefix and gw):
        raise RuntimeError("bearer came up without an IPv4 configuration")

    run = lambda *a: subprocess.run(["sudo", "-n", *a], capture_output=True, text=True)
    run("ip", "link", "set", "wwan0", "up")
    run("ip", "addr", "flush", "dev", "wwan0")
    run("ip", "addr", "add", f"{addr}/{prefix}", "dev", "wwan0")
    if mtu:
        run("ip", "link", "set", "wwan0", "mtu", mtu)
    # Metric 50 beats wifi's 600, so this becomes the default while it exists. Deleted
    # again the moment the WAN comes back.
    run("ip", "route", "replace", "default", "via", gw, "dev", "wwan0", "metric", "50")
    log.warning("cellular data UP: %s/%s via %s", addr, prefix, gw)


def disconnect_data(idx: str) -> None:
    run = lambda *a: subprocess.run(["sudo", "-n", *a], capture_output=True, text=True)
    run("ip", "route", "del", "default", "dev", "wwan0")
    run("ip", "addr", "flush", "dev", "wwan0")
    run("ip", "link", "set", "wwan0", "down")
    try:
        mmcli("-m", idx, "--simple-disconnect", timeout=60)
    except RuntimeError as e:
        log.debug("disconnect: %s", e)
    log.warning("cellular data DOWN, back to the LAN route")


def manage_link(idx: str, server: Server, state: dict) -> None:
    """Hold the data bearer up only while the server says the WAN is down.

    Idempotent and driven by the server's flag rather than by this Pi's own view of the
    network: the machine that needs the internet is the one that should decide when it
    has lost it.
    """
    try:
        wanted = server.link_wanted()
    except Exception as e:
        log.debug("could not read the link flag: %s", e)
        return
    if wanted and not state.get("data_up"):
        try:
            connect_data(idx)
            state["data_up"] = True
        except Exception:
            log.exception("could not bring the cellular data link up")
    elif not wanted and state.get("data_up"):
        disconnect_data(idx)
        state["data_up"] = False


def tick(idx: str, server: Server, state: dict | None = None) -> None:
    if state is not None:
        manage_link(idx, server, state)

    for msg in list_inbound(idx):
        log.info("inbound from %s: %s", msg["number"], msg["text"][:60])
        try:
            server.deliver(msg)
        except Exception as e:
            # Left on the modem deliberately: an undelivered message must survive until
            # the server has actually acknowledged it, or an outage during the one
            # channel meant for outages loses the message it existed to carry.
            log.warning("could not deliver inbound, leaving it on the modem: %s", e)
            continue
        delete_sms(idx, msg["path"])

    for out in server.outbox():
        try:
            send_sms(idx, out["number"], out["text"])
            server.confirm(out["id"], True)
            log.info("sent to %s: %s", out["number"], out["text"][:60])
        except Exception as e:
            log.exception("send failed")
            try:
                server.confirm(out["id"], False, str(e)[:200])
            except Exception:
                log.warning("could not report the send failure either")


UNIT = """[Unit]
Description=Jarvis cellular (SMS) agent
After=network-online.target ModemManager.service
Wants=network-online.target

[Service]
ExecStart={python} {script} --server {server} --key {key}
Restart=always
RestartSec=10
User={user}

[Install]
WantedBy=multi-user.target
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True, help="e.g. http://192.168.0.50:8080")
    ap.add_argument("--key", required=True, help="device_api_key from config.json")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--install", action="store_true", help="print a systemd unit and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.install:
        import getpass
        print(UNIT.format(python=sys.executable, script=__file__, server=args.server,
                          key=args.key, user=getpass.getuser()))
        return 0

    server = Server(args.server, args.key)
    state: dict = {"data_up": False}
    if args.once:
        tick(modem_index(), server, state)
        return 0

    log.info("polling every %ss, server %s", POLL_SECONDS, args.server)
    while True:
        try:
            tick(modem_index(), server, state)
        except Exception:
            # Never exit the loop. This agent is the last channel standing during an
            # outage; dying on a transient modem or network error is the one behaviour
            # it cannot have.
            log.exception("tick failed, continuing")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
