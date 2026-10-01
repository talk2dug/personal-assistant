"""Remove channels whose stream doesn't actually deliver video.

tv_build_channels.py only checked that a stream's playlist answers. Some answer and then
never send video ("ABC News Live 1": playlist 200, 0 bytes of video). This follows an HLS
playlist down to a real media segment and requires actual bytes. Run it monthly too: the
guide notes public streams die all the time.

    python scripts/tv_prune_dead.py --dry-run
    python scripts/tv_prune_dead.py
"""
import argparse
import concurrent.futures as cf
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tv_build_channels import UA  # noqa: E402
from tv_dispatcharr import Dispatcharr  # noqa: E402

MIN_SEGMENT_BYTES = 20_000


def _get(client, url, headers, limit=None):
    with client.stream("GET", url, headers=headers, timeout=10, follow_redirects=True) as r:
        if r.status_code != 200:
            return None, None
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
            if limit and len(body) >= limit:
                break
        return str(r.url), body


def plays(stream: dict) -> bool:
    headers = {"User-Agent": UA}
    cp = stream.get("custom_properties") or {}
    if cp.get("http-referrer"):
        headers["Referer"] = cp["http-referrer"]
    if cp.get("http-user-agent"):
        headers["User-Agent"] = cp["http-user-agent"]
    try:
        with httpx.Client() as c:
            url, body = _get(c, stream["url"], headers, limit=200_000)
            for _ in range(3):          # master -> variant -> segment
                if body is None:
                    return False
                if not body.lstrip().startswith(b"#EXTM3U"):
                    return len(body) >= MIN_SEGMENT_BYTES      # raw TS / progressive stream
                lines = [ln.strip() for ln in body.decode("utf-8", "replace").splitlines()]
                uris = [ln for ln in lines if ln and not ln.startswith("#")]
                if not uris:
                    return False
                nxt = urljoin(url, uris[0] if "#EXT-X-STREAM-INF" in body.decode("utf-8", "replace")
                              else uris[min(1, len(uris) - 1)])
                url, body = _get(c, nxt, headers, limit=MIN_SEGMENT_BYTES * 2)
            return body is not None and len(body) >= MIN_SEGMENT_BYTES
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    d = Dispatcharr()
    streams = {s["id"]: s for s in d.all("/api/channels/streams/")}
    chans = d.all("/api/channels/channels/")

    def check(ch):
        sids = [s if isinstance(s, int) else s.get("id") for s in (ch.get("streams") or [])]
        return ch, any(plays(streams[i]) for i in sids if i in streams)

    with cf.ThreadPoolExecutor(max_workers=30) as pool:
        results = list(pool.map(check, chans))
    dead = [c for c, ok in results if not ok]
    print(f"{len(chans) - len(dead)} of {len(chans)} channels deliver video; {len(dead)} don't")
    for c in dead[:12]:
        print("   dead:", c.get("channel_number"), c.get("name"))
    if dead and not args.dry_run:
        r = d.http.request("DELETE", "/api/channels/channels/bulk-delete/", json={"channel_ids": [c["id"] for c in dead]})
        print("removed dead channels:", r.status_code)


if __name__ == "__main__":
    main()
