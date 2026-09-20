"""RF / Around-the-house dashboard: what the jarvishackrf sensor node hears on the
street, and the one write path for naming a transmitter as the owner's own.

Reads are cheap and come from the cache: the JarvisRadio worker already polls the Pi's
rf_baseline report into jarvis.db's radio_state every ~15 min (see core/radio.py and
radio_main._poll_rf), so the board renders instantly from SQLite and never blocks a page
load on an SSH round trip. A refresh endpoint re-polls on demand for when the owner wants
"right now" rather than "within the quarter hour".

The one thing that DOES reach the Pi on the request path is naming a device: labelling is
a registry write, and the registry lives on the sensor node (sensor_node/rf_sensorctl.py),
so this runs `rf_sensorctl.py label ...` over SSH -- the same fixed-command-shape,
host-by-registered-name pattern the ops-plan workflow uses (core/ssh_ops.py). Every value
the owner supplies is validated against a conservative allow-list and shell-quoted before
it is ever interpolated into that command; the host is the configured radio_rf_host, never
anything a caller names.

Owner-only, same gate as Crypto/Credit/Infra -- this exposes the real street picture
around the house.
"""
import logging
import re
import shlex

from fastapi import APIRouter, HTTPException, Request

from ...core import radio
from ...core.ssh_ops import SSHOpsClient, SSHOpsError
from ..auth import require_owner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rf", tags=["rf"])

# Mirrors sensor_node/rf_sensorctl.py's VALID_TYPES. Kept here (not imported -- that
# module lives on the Pi) so the naming form's dropdown and this validation agree.
VALID_DEVICE_TYPES = ("door", "motion", "smoke", "remote", "water", "temperature", "unknown")

# A fingerprint is model-led ("Secplus-v1/id=-60790566", "Citroen/id=957fdfe1"): it starts
# with an alphanumeric, so argparse never mistakes it for a flag the way a bare negative id
# value ("-60790566") would. That is exactly why the selector is the fingerprint, not the id.
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/=:.+_\-]{0,127}$")


# --------------------------------------------------------------------------- helpers

def _ssh(cfg) -> SSHOpsClient:
    return SSHOpsClient(cfg.ssh_hosts or {})


def _sensorctl_base(cfg) -> tuple[str, str]:
    """(interpreter, rf_sensorctl.py path) derived from radio_rf_baseline_cmd.

    The baseline command already names both the interpreter and the install directory
    ("python3 /home/pi/rf-sensor/rf_baseline.py --json --days 30"); the ctl script is its
    sibling. Deriving it keeps the two in step if the deploy path ever moves in config,
    without a second setting to maintain.
    """
    base = cfg.radio_rf_baseline_cmd or "python3 /home/pi/rf-sensor/rf_baseline.py"
    parts = shlex.split(base)
    interp = parts[0] if parts else "python3"
    script = next((p for p in parts if p.endswith("rf_baseline.py")),
                  "/home/pi/rf-sensor/rf_baseline.py")
    ctl = script.rsplit("/", 1)[0] + "/rf_sensorctl.py" if "/" in script else "rf_sensorctl.py"
    return interp, ctl


def _repoll(cfg) -> dict | None:
    """Run the baseline on the Pi now and fold the result into radio_state, mirroring
    radio_main._poll_rf. Returns the fresh report, or None if the poll did not yield one.
    Raises SSHOpsError on a connection/timeout failure so the caller can report it."""
    host = cfg.radio_rf_host
    if not host or host not in (cfg.ssh_hosts or {}):
        raise SSHOpsError("no RF sensor host is configured (radio_rf_host)")
    res = _ssh(cfg).run_command(host, cfg.radio_rf_baseline_cmd, timeout=90)
    if not res.get("ok"):
        raise SSHOpsError(f"rf_baseline exited {res.get('exit_code')}: "
                          f"{(res.get('output') or '')[:200]}")
    rep = radio.parse_json_object(res.get("output"))
    if not rep or "devices" not in rep:
        raise SSHOpsError("rf_baseline output was not a report")
    radio.set_state(cfg.db_path, "poll:rf", "ok")
    radio.set_state(cfg.db_path, "rf_baseline", rep)
    return rep


def _seen_last_24h(d: dict) -> bool:
    """True if the device was heard in the last 24h. Reads the enriched last_24h block
    when the Pi has been redeployed with it, and falls back to the visits_last_24h count
    the older report shape carries, so the board works before and after that deploy."""
    last24 = d.get("last_24h")
    if isinstance(last24, dict) and last24.get("visits") is not None:
        return last24["visits"] > 0
    return bool(d.get("visits_last_24h"))


def _dashboard(cfg) -> dict:
    db_path = cfg.db_path
    st = radio.feed_status(db_path)
    rep = radio.rf_report(db_path)
    traffic = rep.get("traffic", {}) if rep else {}
    # One-off passing cars (class "vehicle-passing" == a single visit) clutter the table
    # with a row each, so they are dropped here and represented instead by a single
    # summary: the 12h passing total from the baseline plus how many single-pass rows we
    # folded away. Recurring vehicles (vehicle-repeat/regular/watch) and every non-vehicle
    # device stay as their own row.
    devices = []
    hidden_passing = 0
    for d in (rep.get("devices", []) if rep else []):
        if d.get("class") == "vehicle-passing":
            hidden_passing += 1
            continue
        entry = dict(d)
        entry["seen_last_24h"] = _seen_last_24h(d)
        devices.append(entry)
    return {
        "feed": {
            "available": rep is not None,
            "stale": st["rf_stale"],
            "poll_age_s": st["rf_poll_age_s"],
            "host": cfg.radio_rf_host,
            "generated_at": rep.get("generated_at") if rep else None,
            "window_days": rep.get("window_days") if rep else None,
        },
        "counts": rep.get("counts", {}) if rep else {},
        "traffic": traffic,
        # Summary of the passing cars folded out of the table above. passing_12h is the
        # total cars that drove by in the last 12h (from the baseline; None until a Pi
        # redeploy + re-poll lands the new field), hidden_passing is how many single-pass
        # rows we removed from this response.
        "passing_summary": {
            "passing_12h": traffic.get("passing_12h"),
            "hidden_passing": hidden_passing,
        },
        "alerts": rep.get("alerts", []) if rep else [],
        "devices": devices,
        "device_types": list(VALID_DEVICE_TYPES),
    }


def _clean_text(value, field: str, *, required: bool, max_len: int = 64) -> str | None:
    value = (value or "").strip()
    if not value:
        if required:
            raise HTTPException(400, f"{field} is required")
        return None
    if len(value) > max_len:
        raise HTTPException(400, f"{field} must be {max_len} characters or fewer")
    if value[0] == "-":
        raise HTTPException(400, f"{field} must not start with '-'")
    if any(ord(c) < 32 for c in value):
        raise HTTPException(400, f"{field} contains control characters")
    return value


# --------------------------------------------------------------------------- routes

@router.get("/dashboard")
async def dashboard(request: Request):
    """The whole street picture in one cheap read: 24h device table, class counts, vehicle
    traffic and feed health, all from the cached baseline report -- no SSH on this path."""
    require_owner(request)
    return _dashboard(request.app.state.cfg)


@router.post("/refresh")
async def refresh(request: Request):
    """Re-poll the sensor node right now and return the fresh dashboard. This is the one
    read that reaches the Pi, so it is a separate, explicit action rather than the default."""
    require_owner(request)
    cfg = request.app.state.cfg
    try:
        _repoll(cfg)
    except SSHOpsError as e:
        raise HTTPException(502, f"could not refresh the RF sensor node: {e}")
    except Exception:
        logger.exception("rf: refresh failed")
        raise HTTPException(502, "could not refresh the RF sensor node")
    return _dashboard(cfg)


@router.post("/devices/label")
async def label_device(request: Request):
    """Flag & name a transmitter as the owner's own -- the write path Jack asked for.

    Labelling a device is what turns it into class "own" on the node (rf_sensorctl label
    sets registered = 1). We run that CLI over SSH with the fingerprint as the selector and
    every field validated + shell-quoted, then re-poll so the board reflects the new name
    immediately rather than at the next 15-minute tick.
    """
    require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()

    selector = (body.get("fingerprint") or body.get("selector") or "").strip()
    if not _FINGERPRINT_RE.match(selector):
        raise HTTPException(400, "invalid device fingerprint")
    name = _clean_text(body.get("name"), "name", required=True)
    location = _clean_text(body.get("location"), "location", required=False)
    dtype = (body.get("type") or body.get("device_type") or "unknown").strip() or "unknown"
    if dtype not in VALID_DEVICE_TYPES:
        raise HTTPException(400, f"type must be one of: {', '.join(VALID_DEVICE_TYPES)}")

    host = cfg.radio_rf_host
    if not host or host not in (cfg.ssh_hosts or {}):
        raise HTTPException(503, "no RF sensor host is configured")

    interp, ctl = _sensorctl_base(cfg)
    parts = [shlex.quote(interp), shlex.quote(ctl), "label",
             shlex.quote(selector), shlex.quote(name), "--type", shlex.quote(dtype)]
    if location:
        parts += ["--location", shlex.quote(location)]
    command = " ".join(parts)

    try:
        res = _ssh(cfg).run_command(host, command, timeout=30)
    except SSHOpsError as e:
        raise HTTPException(502, f"could not reach the RF sensor node: {e}")
    except Exception:
        logger.exception("rf: label command failed")
        raise HTTPException(502, "could not reach the RF sensor node")

    output = (res.get("output") or "").strip()
    if not res.get("ok"):
        # A non-zero exit is a real result (e.g. the selector matched nothing or was
        # ambiguous) -- surface the CLI's own message rather than a generic failure.
        raise HTTPException(422, output or "rf_sensorctl could not label that device")

    # Best-effort re-poll so "own" shows straight away; a labelled device that still reads
    # as unknown until the next tick would look like the flag did not take.
    dash = None
    try:
        _repoll(cfg)
        dash = _dashboard(cfg)
    except Exception:
        logger.warning("rf: re-poll after label failed; board will catch up next tick")

    return {"ok": True, "message": output, "fingerprint": selector,
            "name": name, "location": location, "type": dtype,
            "dashboard": dash}
