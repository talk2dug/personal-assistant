"""TP-Link Omada Open API client -- network visibility (devices, clients, health) plus a
narrow, deliberately small set of control actions for Jarvis.

Read tools (list devices/clients, device health) are safe and freely callable, same
reasoning as `ssh_health.py`'s plain reachability check. Anything that changes real network
state goes through `engine.py`'s `sensitive_tools` confirmation gate -- the same mechanism
already used for `git_merge_pr`, Home Assistant's lock/cover/alarm domains, Kroger checkout,
and CCXT trades -- not `ops_plans.py`'s SSH-specific multi-step executor, which is built for
scripted server changes with a rollback phase, not a single atomic action like "reboot this
switch". See `docs/omada-network-design.md`.

Auth is OAuth2 client-credentials, registered once by the owner on the controller's own
Settings -> Open API screen -- never a raw username/password. Two real, TP-Link-specific
quirks confirmed live against a real controller (firmware 5.14.32.56, API v3) before writing
this, not assumed from vendor docs (which don't give exact request shapes):

1. The authenticated `Authorization` header is `AccessToken=<token>`, NOT the standard
   `Bearer <token>` -- despite the token response's own `tokenType` field saying "bearer".
   Confirmed by testing both: `Bearer` gets a token-expired-shaped error even for a
   brand-new token, `AccessToken=` succeeds.
2. Every authenticated URL is namespaced under `/openapi/v1/{omadacId}/...` -- omadacId is
   NOT something to derive or guess; it's the controller's own internal identifier
   (available unauthenticated at `GET /api/info`, or from the Open API registration
   screen), and a wrong grant_type/params shape at the token endpoint returns a distinctly
   different, generic error than a wrong credential does -- useful for telling "I have the
   request shape wrong" apart from "the credentials themselves are bad" if this ever needs
   re-diagnosing.
"""
import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)


class OmadaError(Exception):
    pass


class OmadaClient:
    """One client per controller. Holds its own access/refresh token pair and re-
    authenticates transparently; callers never see token plumbing."""

    def __init__(self, controller_url: str, client_id: str, client_secret: str,
                 omada_id: str, site_id: str, db_path: str | None = None, timeout: float = 15.0):
        self.controller_url = controller_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.omada_id = omada_id
        self.site_id = site_id
        # Only needed for call_tool's get_omada_network_health branch, which reads the
        # recorded device/client history omada_health.py owns -- everything else here
        # talks to the controller directly and never touches SQLite.
        self.db_path = db_path
        self._http = httpx.Client(base_url=self.controller_url, timeout=timeout, verify=False)
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0

    # -- auth -----------------------------------------------------------------

    def _authenticate(self) -> None:
        """The initial client-credentials grant. Called once, and again only if a refresh
        itself fails (e.g. the refresh token has also expired)."""
        resp = self._http.post(
            "/openapi/authorize/token", params={"grant_type": "client_credentials"},
            json={"omadacId": self.omada_id, "client_id": self.client_id,
                  "client_secret": self.client_secret},
        )
        data = resp.json()
        if data.get("errorCode") != 0:
            raise OmadaError(f"authentication failed: {data.get('msg')} (errorCode={data.get('errorCode')})")
        result = data["result"]
        self._access_token = result["accessToken"]
        self._refresh_token = result.get("refreshToken")
        # A safety margin, not the literal expiry -- a call that starts just before the
        # token dies must not fail mid-flight rather than refreshing a little early.
        self._expires_at = time.monotonic() + result.get("expiresIn", 7200) - 60

    def _refresh(self) -> None:
        if not self._refresh_token:
            self._authenticate()
            return
        resp = self._http.post(
            "/openapi/authorize/token", params={"grant_type": "refresh_token"},
            json={"omadacId": self.omada_id, "refreshToken": self._refresh_token,
                  "client_id": self.client_id, "client_secret": self.client_secret},
        )
        data = resp.json()
        if data.get("errorCode") != 0:
            # A dead refresh token is not an error worth surfacing -- fall back to a fresh
            # client-credentials grant, same end state either way.
            logger.info("Omada refresh token no longer valid, re-authenticating from scratch")
            self._authenticate()
            return
        result = data["result"]
        self._access_token = result["accessToken"]
        self._refresh_token = result.get("refreshToken", self._refresh_token)
        self._expires_at = time.monotonic() + result.get("expiresIn", 7200) - 60

    def _ensure_token(self) -> str:
        with self._lock:
            if self._access_token is None:
                self._authenticate()
            elif time.monotonic() >= self._expires_at:
                self._refresh()
            return self._access_token

    def _request(self, method: str, path: str, retry_on_expired: bool = True, **kwargs) -> dict:
        token = self._ensure_token()
        resp = self._http.request(method, path, headers={"Authorization": f"AccessToken={token}"}, **kwargs)
        data = resp.json()
        # -44112 is this controller's own "access token has expired" code (confirmed
        # live) -- distinct from a request simply being malformed, so this is the one
        # case worth one transparent retry rather than surfacing an error the caller
        # can do nothing about.
        if data.get("errorCode") == -44112 and retry_on_expired:
            with self._lock:
                self._refresh()
            return self._request(method, path, retry_on_expired=False, **kwargs)
        if data.get("errorCode") not in (0, None):
            raise OmadaError(f"{method} {path} failed: {data.get('msg')} (errorCode={data.get('errorCode')})")
        return data.get("result", {})

    def _site_path(self, suffix: str) -> str:
        return f"/openapi/v1/{self.omada_id}/sites/{self.site_id}/{suffix}"

    # -- read -------------------------------------------------------------------

    def list_devices(self, page_size: int = 100) -> list[dict]:
        """Every AP/switch/gateway the controller manages at this site."""
        result = self._request("GET", self._site_path("devices"),
                               params={"page": 1, "pageSize": page_size})
        return result.get("data", [])

    def list_clients(self, page_size: int = 200) -> list[dict]:
        """Every client currently on the network -- wired and wireless."""
        result = self._request("GET", self._site_path("clients"),
                               params={"page": 1, "pageSize": page_size})
        return result.get("data", [])

    # -- control (gated -- see engine.py's sensitive_tools dispatch) -------------

    def reboot_device(self, mac: str) -> dict:
        """Reboots one device by MAC. The one control action this ships with -- see the
        module docstring and docs/omada-network-design.md for why the control surface
        starts this narrow."""
        return self._request("POST", self._site_path("cmd/devices/reboot"), json={"macList": [mac]})

    # -- dispatch -----------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Same call_tool(name, arguments) shape as every other integration (GitOpsClient,
        etc.) -- engine.py's tool dispatch and its pending-actions confirmation resolver
        both call through this uniformly, never the named methods directly."""
        from . import omada_health

        if name == "list_omada_devices":
            return {"devices": self.list_devices()}
        if name == "list_omada_clients":
            return {"clients": self.list_clients()}
        if name == "get_omada_network_health":
            if self.db_path is None:
                return {"error": "no db_path configured for this client"}
            return omada_health.feed_status(self.db_path)
        if name == "reboot_omada_device":
            mac = arguments.get("mac")
            if not mac:
                return {"ok": False, "error": "mac is required"}
            return {"ok": True, "result": self.reboot_device(mac)}
        return {"error": f"unknown omada tool {name!r}"}
