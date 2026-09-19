"""Keep Jarvis on the internet when the house WAN dies, by borrowing the LTE modem.

Everything Jarvis needs the internet for happens on ONE machine. Claude (the brain),
Era, mail, Kroger, the market feed, Telegram -- all of it originates on jarvisbox. Home
Assistant, Ollama, the cameras and the voice terminals are all on the LAN and do not care
whether the WAN exists. So this is not whole-house failover; it is getting one process
back online, which is a far smaller and safer problem.

The mechanism is a proxy, not a route. jarvisaudio2 runs tinyproxy over its cellular
link, and when the WAN is down this points Jarvis's outbound calls at it by setting the
standard HTTPS_PROXY/HTTP_PROXY variables. That choice is deliberate and the whole reason
this design was picked over making the Pi a gateway:

  * Nothing touches the routing table on the owner's main machine. A failover bug can
    degrade Jarvis; it cannot strand the desktop he is sitting at.
  * It needs no administrator rights and no reboot.
  * Only Jarvis's traffic crosses the metered link. A NAT default route would put the
    whole box on cellular, including whatever else happens to be running.
  * It reverts by itself: clear two environment variables and everything is as it was.

httpx, requests and urllib all honour those variables (httpx reads them when a client is
constructed, so new connections pick the change up), and the Claude CLI subprocess
inherits them from the environment. That is the entire integration surface.

FAILING SAFE MEANS FAILING DIRECT. Every uncertain case here leaves traffic on the normal
path: if the proxy cannot be reached, if the modem never connects, if the check itself
errors. A broken failover must never be worse than no failover, because the thing it
protects is the assistant's only way of answering at all.
"""
import logging
import os
import socket
import urllib.request

from . import db

logger = logging.getLogger(__name__)

# Whether the Pi should be holding its cellular data bearer up. Persisted rather than
# held in memory so the intent survives a restart of either side -- an outage is exactly
# when a process is most likely to have been restarted.
LINK_WANTED_KEY = "cellular_data_wanted"

# Probed to decide whether the WAN is alive. Several, on different operators, because
# one of them being down is not an outage and must not trigger failover.
WAN_PROBES = [("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443)]
PROBE_TIMEOUT = 3.0

PROXY_ENV_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
# Never proxy the LAN. Home Assistant, Ollama, the terminals and the Pi itself are all
# local; sending that traffic out over cellular and back would be absurd, and during an
# outage it is the local half that still works.
NO_PROXY_VALUE = "localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,*.local"


def wan_reachable(probes=None, timeout: float = PROBE_TIMEOUT) -> bool:
    """Can this machine reach the internet directly, right now?

    A TCP connect rather than an HTTP request or a ping: ICMP is filtered in plenty of
    places, and a full request would also fail when the far service is merely having a
    bad day. One successful handshake to any probe means the WAN is up.
    """
    for host, port in (probes or WAN_PROBES):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def proxy_reachable(proxy_url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether the failover proxy is actually listening. Checked before switching to it,
    so a dead Pi cannot take Jarvis off a working connection."""
    try:
        host, _, port = proxy_url.split("//", 1)[1].partition(":")
        with socket.create_connection((host, int(port or 8888)), timeout=timeout):
            return True
    except (OSError, ValueError, IndexError):
        return False


def proxy_active() -> str | None:
    return os.environ.get("HTTPS_PROXY")


def set_proxy(proxy_url: str) -> None:
    for var in PROXY_ENV_VARS:
        os.environ[var] = proxy_url
    os.environ["NO_PROXY"] = NO_PROXY_VALUE
    os.environ["no_proxy"] = NO_PROXY_VALUE
    logger.warning("WAN failover: routing outbound traffic via %s", proxy_url)


def clear_proxy() -> None:
    for var in (*PROXY_ENV_VARS, "NO_PROXY", "no_proxy"):
        os.environ.pop(var, None)
    logger.warning("WAN failover: back on the normal connection")


def link_wanted(db_path: str) -> bool:
    return db.get_setting(db_path, LINK_WANTED_KEY, "0") == "1"


def set_link_wanted(db_path: str, wanted: bool) -> None:
    db.set_setting(db_path, LINK_WANTED_KEY, "1" if wanted else "0")


def internet_via_proxy(proxy_url: str, timeout: float = 15.0) -> bool:
    """Prove the proxy can actually carry traffic, not merely that it is listening.

    Worth the extra round trip: tinyproxy answers on its port whether or not the modem
    behind it has a bearer, so "the port is open" is not the same claim as "this will
    get Jarvis to Claude".
    """
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url}))
    try:
        with opener.open("https://1.1.1.1/cdn-cgi/trace", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def evaluate(db_path: str, proxy_url: str | None, enabled: bool = True) -> dict:
    """One decision about how Jarvis should be reaching the internet.

    Called on a timer. Returns what it saw and what it did, so the caller can log it and
    the Command Center can show it without re-deriving anything.
    """
    if not enabled or not proxy_url:
        if proxy_active():
            clear_proxy()
        return {"enabled": False, "wan": None, "using_proxy": False}

    wan = wan_reachable()
    using = bool(proxy_active())

    if wan:
        # Normal service. Drop the proxy and tell the Pi it can release the bearer --
        # holding a cellular link up costs data allowance and battery for nothing.
        if using:
            clear_proxy()
        if link_wanted(db_path):
            set_link_wanted(db_path, False)
        return {"enabled": True, "wan": True, "using_proxy": False, "changed": using}

    # WAN is down. Ask for the bearer first: the Pi polls that flag, so the link has to
    # be requested before there is any point testing the proxy behind it.
    if not link_wanted(db_path):
        set_link_wanted(db_path, True)
        logger.warning("WAN failover: WAN is down, asked for the cellular link")

    if using:
        return {"enabled": True, "wan": False, "using_proxy": True, "changed": False}

    if not proxy_reachable(proxy_url):
        # Fail direct: better to keep trying the dead WAN than to point Jarvis at a
        # proxy that is not there.
        return {"enabled": True, "wan": False, "using_proxy": False,
                "note": "proxy not reachable"}
    if not internet_via_proxy(proxy_url):
        return {"enabled": True, "wan": False, "using_proxy": False,
                "note": "proxy reachable but has no internet yet"}

    set_proxy(proxy_url)
    return {"enabled": True, "wan": False, "using_proxy": True, "changed": True}
