"""Build the curated "US Live" lineup in Dispatcharr from the iptv-org streams.

Following PeiceOfToast's guide (Part 3, channel curation), with one addition: every stream
is probed first, and only ones that answer with a real playlist/video become channels --
the guide warns many public streams are dead or geo-blocked, and a lineup full of dead
channels is worse than a short one.

    python scripts/tv_build_channels.py            # probe + build
    python scripts/tv_build_channels.py --dry-run  # probe + report only
"""
import argparse
import concurrent.futures as cf
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tv_dispatcharr import Dispatcharr  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
PROFILE = "US Live"

# Channel number blocks by the FIRST tag of a group ("News;Sports" -> News). The guide's
# 100-wide blocks overflowed on the real list (240 local/general, 259 entertainment), so the
# two big categories get their own ranges up top. (start, size, tags)
BLOCKS = [
    (100, 100, ("News",)),
    (200, 100, ("Sports", "Outdoor", "Auto")),
    (300, 100, ("Movies",)),
    (400, 100, ("Kids", "Animation", "Family")),
    (500, 100, ("Documentary", "Science", "Education", "Culture")),
    (600, 100, ("Lifestyle", "Cooking", "Travel")),
    (700, 100, ("Music", "Relax")),
    (1000, 1000, ("Entertainment", "Series", "Comedy", "Classic")),
    (2000, 1000, ("Weather", "General", "Business")),
]
SIZE_OF = {start: size for start, size, _ in BLOCKS}
BLOCK_OF = {tag: start for start, _size, tags in BLOCKS for tag in tags}


def alive(stream: dict) -> bool:
    headers = {"User-Agent": UA}
    cp = stream.get("custom_properties") or {}
    for k in ("http-referrer", "referrer"):
        if cp.get(k):
            headers["Referer"] = cp[k]
    if cp.get("http-user-agent"):
        headers["User-Agent"] = cp["http-user-agent"]
    try:
        with httpx.stream("GET", stream["url"], headers=headers, timeout=8, follow_redirects=True) as r:
            if r.status_code != 200:
                return False
            ctype = r.headers.get("content-type", "").lower()
            head = b""
            for chunk in r.iter_bytes():
                head += chunk
                if len(head) >= 2048:
                    break
            return (b"#EXTM3U" in head or "mpegurl" in ctype or "video" in ctype or "mp2t" in ctype
                    or head[:1] == b"G")    # 0x47: MPEG-TS sync byte
    except Exception:
        return False


def base_name(name: str) -> str:
    """'ABC News (1080p) [Geo-blocked]' -> 'abc news': one channel per real channel."""
    n = re.sub(r"\s*[\(\[].*?[\)\]]", "", name or "")
    return re.sub(r"\s+", " ", n).strip().lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    d = Dispatcharr()
    groups = {g["id"]: g["name"] for g in d.all("/api/channels/groups/")}
    streams = [s for s in d.all("/api/channels/streams/") if not s.get("is_adult")]
    print(f"probing {len(streams)} streams...")
    with cf.ThreadPoolExecutor(max_workers=40) as pool:
        live = [s for s, ok in zip(streams, pool.map(alive, streams)) if ok]
    print(f"live: {len(live)} of {len(streams)}")

    # One channel per real channel: prefer the highest resolution variant that answered.
    def res(s):
        m = re.search(r"(\d{3,4})p", s["name"])
        return int(m.group(1)) if m else 0
    best = {}
    for s in sorted(live, key=res, reverse=True):
        best.setdefault(base_name(s["name"]), s)
    picked = list(best.values())

    by_block = {}
    for s in picked:
        first_tag = (groups.get(s["channel_group"]) or "General").split(";")[0]
        by_block.setdefault(BLOCK_OF.get(first_tag, 2000), []).append(s)
    for start in sorted(by_block):
        names = sorted(by_block[start], key=lambda s: s["name"].lower())
        by_block[start] = names
        if len(names) > SIZE_OF[start]:
            raise SystemExit(f"block {start} overflows: {len(names)} channels for {SIZE_OF[start]} numbers")
        print(f"  {start}-{start + len(names) - 1}: {len(names)} channels, e.g. {', '.join(s['name'] for s in names[:4])}")
    if args.dry_run:
        return

    prof = next((p for p in d.all("/api/channels/profiles/") if p["name"] == PROFILE), None)
    if prof is None:
        prof = d.post("/api/channels/profiles/", {"name": PROFILE, "start_empty": True})
    for start, ss in sorted(by_block.items()):
        # Each block has 100 numbers; a bigger block spills, which the numbering shows.
        d.post("/api/channels/channels/from-stream/bulk/", {
            "stream_ids": [s["id"] for s in ss], "channel_profile_ids": [prof["id"]],
            "starting_channel_number": start})
    chans = d.all("/api/channels/channels/")
    print(f"created {len(chans)} channels in profile '{PROFILE}' (id {prof['id']})")
    r = d.post("/api/channels/channels/match-epg/", {"channel_ids": [c["id"] for c in chans]})
    print("EPG auto-match:", str(r)[:200])


if __name__ == "__main__":
    main()
