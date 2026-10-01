"""After the first iptv-org guide grab: add it to Dispatcharr as the primary guide source
(file /data/epgs/guide.xml.gz inside the container = /opt/tv/dispatcharr/epgs on tism),
refresh it, and re-match every channel. Idempotent."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tv_dispatcharr import Dispatcharr  # noqa: E402

d = Dispatcharr()
name = "iptv-org grabber (nightly)"
src = next((s for s in d.all("/api/epg/sources/") if s["name"] == name), None)
if src is None:
    src = d.post("/api/epg/sources/", {"name": name, "source_type": "xmltv",
                                       "file_path": "/data/epgs/guide.xml.gz",
                                       "is_active": True, "refresh_interval": 12, "priority": 10})
    print("added guide source", src["id"])
src = d.wait(f"/api/epg/sources/{src['id']}/", timeout=1800)
print("guide source:", src.get("status"), src.get("epg_data_count"), (src.get("last_message") or "")[:120])
chans = d.all("/api/channels/channels/")
d.post("/api/channels/channels/match-epg/", {"channel_ids": [c["id"] for c in chans]})
prev = -1
for _ in range(40):
    time.sleep(15)
    m = sum(1 for c in d.all("/api/channels/channels/") if c.get("epg_data_id"))
    if m == prev:
        break
    prev = m
print(f"channels with guide data: {m} of {len(chans)}")
