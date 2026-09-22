"""Pull the images Jack has already made in Leonardo.Ai into the design catalogue.

*"I'd rather not go through and individually download the hundreds of images that I've
made to import them into our media catalogue."*

So this pages his generations and files each one as a design asset, ready to pick from
later -- the same shelf the artwork he photographs lands on.

TWO THINGS TO KNOW BEFORE RUNNING IT.

**The API is billed separately from the web app.** Leonardo's API plans are their own
track; subscription tokens do not pay for API calls and vice versa. Every account gets
$5 of API credit that does not expire, and credits are spent GENERATING, not listing or
downloading -- so importing what already exists should cost nothing or close to it. It
still needs an API key from the API Access page.

**Whether an API key can see WEB APP generations is undocumented.** Leonardo's own FAQ
says images made via the API are visible in the web app, and is silent on the reverse,
which is the direction that matters here. Rather than guess, `probe` asks his account
the question directly and reports what came back. If the answer is no, that is worth
knowing in ten seconds rather than after building an importer around an assumption.

NOTHING HERE GENERATES ANYTHING. Every call is a GET. A module that could spend his
credits while "importing" would be a bad trade for a convenience.
"""
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

BASE = "https://cloud.leonardo.ai/api/rest/v1"

# Leonardo pages generations; 50 is its documented ceiling per call.
PAGE = 50


class LeonardoError(RuntimeError):
    """The API refused or could not be reached."""


class LeonardoClient:
    def __init__(self, api_key: str, base: str = BASE, timeout: float = 30.0):
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict:
        import httpx

        try:
            response = httpx.get(
                f"{self.base}{path}", params=params or {},
                headers={"accept": "application/json",
                         "authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout)
        except Exception as exc:                                    # noqa: BLE001
            raise LeonardoError(f"could not reach Leonardo: {exc}") from exc
        if response.status_code == 401:
            raise LeonardoError("Leonardo rejected the API key (401)")
        if response.status_code == 403:
            raise LeonardoError(
                "Leonardo refused the request (403) -- this usually means the account "
                "has no API plan, which is billed separately from the web subscription")
        if response.status_code >= 400:
            raise LeonardoError(f"Leonardo returned {response.status_code}: "
                                f"{response.text[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise LeonardoError("Leonardo returned something that was not JSON") from exc

    def user_id(self) -> str:
        """His Leonardo user id, which every generations call needs."""
        data = self._get("/me")
        details = data.get("user_details") or []
        if not details:
            raise LeonardoError("Leonardo did not return a user for this key")
        user = (details[0] or {}).get("user") or {}
        found = user.get("id")
        if not found:
            raise LeonardoError("Leonardo returned a user with no id")
        return str(found)

    def generations(self, user_id: str, offset: int = 0, limit: int = PAGE) -> list:
        data = self._get(f"/generations/user/{user_id}",
                         {"offset": offset, "limit": limit})
        return data.get("generations") or []


def images_in(generation: dict) -> list:
    """Every finished image in one generation, as {id, url, prompt, created_at}.

    Defensive about shape on purpose: this is somebody else's API, the field names have
    moved before, and a rename should cost him a skipped image rather than a crash
    halfway through several hundred.
    """
    out = []
    prompt = (generation.get("prompt") or "").strip()
    created = generation.get("createdAt") or generation.get("created_at")
    for image in (generation.get("generated_images") or []):
        url = image.get("url") or image.get("motionMP4URL")
        if not url:
            continue
        out.append({"id": str(image.get("id") or url), "url": url,
                    "prompt": prompt, "created_at": created})
    return out


def probe(client: LeonardoClient) -> dict:
    """Answer the one undocumented question: can this key see his existing work?

    Returns {"ok", "user_id", "generations", "images", "sample_prompt", "error"}. Cheap
    -- one /me and one page -- and it settles in seconds what the documentation does not
    say at all.
    """
    try:
        uid = client.user_id()
    except LeonardoError as exc:
        return {"ok": False, "error": str(exc), "user_id": None,
                "generations": 0, "images": 0, "sample_prompt": None}
    try:
        page = client.generations(uid, offset=0, limit=PAGE)
    except LeonardoError as exc:
        return {"ok": False, "error": str(exc), "user_id": uid,
                "generations": 0, "images": 0, "sample_prompt": None}

    images = [i for g in page for i in images_in(g)]
    return {"ok": True, "error": None, "user_id": uid,
            "generations": len(page), "images": len(images),
            "sample_prompt": (images[0]["prompt"][:120] if images else None)}


def _safe_name(prompt: str, image_id: str) -> str:
    """A filename he could recognise in a folder listing.

    Named from the prompt, because that is what he will remember about an image he made
    months ago -- 'skull-in-a-top-hat' rather than a UUID. The id is kept on the end so
    two images from one prompt cannot collide.
    """
    stem = re.sub(r"[^A-Za-z0-9 _-]", "", prompt or "").strip()
    stem = re.sub(r"\s+", "-", stem)[:60].strip("-") or "leonardo"
    return f"{stem}-{image_id[:8]}.jpg"


def import_generations(db_path: str, owner_user_id: int, client: LeonardoClient,
                       media_path: str, max_images: int = 500,
                       download=None) -> dict:
    """Page his generations and file every new image as a design asset.

    Safe to run again: an image already in the catalogue is skipped by its Leonardo id,
    so a second run picks up only what is new rather than duplicating several hundred
    designs. A single failed download is logged and skipped -- one bad image should not
    end an import of hundreds.
    """
    from . import design_assets

    design_assets.init_design_assets(db_path)
    out_dir = os.path.join(media_path, "leonardo")
    os.makedirs(out_dir, exist_ok=True)

    try:
        uid = client.user_id()
    except LeonardoError as exc:
        return {"ok": False, "error": str(exc), "imported": 0, "skipped": 0, "failed": 0}

    imported = skipped = failed = 0
    offset = 0
    while imported + skipped < max_images:
        try:
            page = client.generations(uid, offset=offset, limit=PAGE)
        except LeonardoError as exc:
            logger.warning("leonardo: stopped paging at offset %d: %s", offset, exc)
            break
        if not page:
            break
        offset += len(page)

        for generation in page:
            for image in images_in(generation):
                if imported + skipped >= max_images:
                    break
                if design_assets.has_external(db_path, owner_user_id, image["id"]):
                    skipped += 1
                    continue
                target = os.path.join(out_dir, _safe_name(image["prompt"], image["id"]))
                try:
                    (download or _download)(image["url"], target)
                except Exception:
                    logger.warning("leonardo: could not download %s", image["id"],
                                   exc_info=True)
                    failed += 1
                    continue
                design_assets.add(
                    db_path, owner_user_id, target,
                    title=(image["prompt"][:80] or None), source="leonardo",
                    note=image["prompt"] or None, external_id=image["id"])
                imported += 1

    return {"ok": True, "error": None, "imported": imported, "skipped": skipped,
            "failed": failed}


def _download(url: str, target: str) -> None:
    import httpx

    with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as response:
        response.raise_for_status()
        with open(target, "wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
