"""The shared HTTP manners both storefront clients need.

Printify and Shopify are different APIs with the same three hazards, so the handling lives
here once rather than being written twice and drifting:

  * **They throttle, and they tell you how long to wait.** Shopify's Basic plan refills a
    40-call bucket at 2 calls/second; Printify allows 600/minute but only 200 publishes per
    half hour. Both answer 429 with Retry-After. Honouring that header is the difference
    between a job that finishes slowly and one that half-finishes and lies about it -- the
    607-product purge ran clean precisely because it waited when told to.
  * **A missing credential is the normal state here, not an exception.** Every one of these
    tokens starts life as a request on Jack's board, so "not supplied yet" has to produce
    an error that names the board entry rather than a KeyError three frames deep.
  * **Credentials are read at USE time.** Never cached on the instance, never read at
    process start: a token pasted into the board at noon has to work on the 12:05 run
    without restarting a Windows service.
"""
import json
import logging
import time
import urllib.error
import urllib.request

from . import owner_requests

logger = logging.getLogger(__name__)

# How many times to re-attempt a call that was throttled or failed transiently. Five is
# enough to ride out a full Shopify bucket refill without turning a broken credential into
# a thirty-second hang.
MAX_ATTEMPTS = 5

# What to wait when a 429 arrives without a Retry-After header, which does happen.
DEFAULT_BACKOFF_S = 2.0


class StoreCredentialMissing(RuntimeError):
    """A credential this client needs has not been supplied yet.

    Carries the board request name so the caller can say which entry is blocking it,
    instead of reporting a generic auth failure that looks like a broken integration.
    """

    def __init__(self, name: str, service: str):
        self.name = name
        self.service = service
        super().__init__(
            f"{service} is not configured: no value for '{name}'. It is waiting on the "
            f"owner's Needs You board -- report that you are blocked on it rather than "
            f"working around it."
        )


class StoreAPIError(RuntimeError):
    """A call reached the service and the service refused it."""

    def __init__(self, service: str, status: int, body: str, path: str):
        self.service, self.status, self.body, self.path = service, status, body, path
        super().__init__(f"{service} {status} on {path}: {body[:300]}")


def require_secret(db_path: str, name: str, service: str, config_fallback: str | None = None) -> str:
    """The credential, from the board first and config second.

    The board wins deliberately: replacing a dead token is then something Jack does from a
    screen, not by editing a file he has never opened. config_fallback exists only so
    credentials that predate the board keep working.
    """
    value = owner_requests.secret(db_path, name) or config_fallback
    if not value:
        raise StoreCredentialMissing(name, service)
    return value


# When each service was last called, so `min_interval_s` can pace a loop across separate
# client instances. Module level rather than a mutable default argument: the pacing really
# is process-wide state, and hiding process-wide state in a function signature is how it
# ends up impossible to reason about or reset.
_LAST_CALL: dict[str, float] = {}


def request_json(service: str, url: str, *, headers: dict, method: str = "GET",
                 body: dict | None = None, timeout: int = 30,
                 min_interval_s: float = 0.0) -> tuple:
    """One API call, with throttling honoured and 429s waited out.

    Returns (parsed_json, response_headers). A 204/empty body parses as None rather than
    raising, because several of these endpoints answer a successful DELETE with nothing.

    `min_interval_s` paces calls per service so a loop cannot burst through a rate limit
    in the first second and then spend the rest of the job in back-off.
    """
    payload = json.dumps(body).encode() if body is not None else None
    last = _LAST_CALL.get(service)
    if last is not None and min_interval_s:
        gap = min_interval_s - (time.monotonic() - last)
        if gap > 0:
            time.sleep(gap)

    for attempt in range(MAX_ATTEMPTS):
        req = urllib.request.Request(url, data=payload, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                _LAST_CALL[service] = time.monotonic()
                raw = response.read()
                return (json.loads(raw) if raw else None), dict(response.headers)
        except urllib.error.HTTPError as exc:
            _LAST_CALL[service] = time.monotonic()
            text = exc.read().decode("utf-8", "replace")
            if exc.code == 429 and attempt < MAX_ATTEMPTS - 1:
                wait = float(exc.headers.get("Retry-After") or DEFAULT_BACKOFF_S)
                logger.info("%s throttled on %s; waiting %.1fs as instructed", service, url, wait)
                time.sleep(wait + 0.25)
                continue
            # 5xx is the service having a moment, not the request being wrong.
            if 500 <= exc.code < 600 and attempt < MAX_ATTEMPTS - 1:
                time.sleep(DEFAULT_BACKOFF_S * (attempt + 1))
                continue
            raise StoreAPIError(service, exc.code, text, url) from None
        except urllib.error.URLError as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise StoreAPIError(service, 0, f"unreachable: {exc.reason}", url) from None
            time.sleep(DEFAULT_BACKOFF_S * (attempt + 1))
    raise StoreAPIError(service, 0, "exhausted retries", url)
