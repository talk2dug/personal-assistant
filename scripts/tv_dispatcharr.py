"""Small Dispatcharr API client for the TV setup on tism (see config.json "tv").

    python scripts/tv_dispatcharr.py status
Import it for the setup steps; everything goes through Dispatcharr's own REST API.
"""
import json
import sys
import time
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG = json.load(open(Path(__file__).resolve().parent.parent / "config.json", encoding="utf-8"))["tv"]
BASE = CFG["dispatcharr_url"]


class Dispatcharr:
    def __init__(self):
        self.http = httpx.Client(base_url=BASE, timeout=120)
        tok = self.http.post("/api/accounts/token/", json={
            "username": CFG["dispatcharr_user"], "password": CFG["dispatcharr_password"]}).json()["access"]
        self.http.headers["Authorization"] = f"Bearer {tok}"

    def get(self, path, **params):
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path, body):
        r = self.http.post(path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else {}

    def patch(self, path, body):
        r = self.http.patch(path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"PATCH {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else {}

    def all(self, path, **params):
        """Every item from a (possibly paginated) list endpoint."""
        out, page = [], 1
        while True:
            d = self.get(path, page=page, page_size=500, **params)
            if isinstance(d, list):
                return d
            out += d.get("results", [])
            if not d.get("next"):
                return out
            page += 1

    def wait(self, path, key="status", done=("success",), timeout=900):
        start = time.time()
        while time.time() - start < timeout:
            obj = self.get(path)
            if obj.get(key) in done or obj.get(key) == "error":
                return obj
            time.sleep(10)
        raise TimeoutError(path)


if __name__ == "__main__":
    d = Dispatcharr()
    for a in d.all("/api/m3u/accounts/"):
        print("M3U", a["id"], a["name"], a.get("status"), (a.get("last_message") or "")[:80])
    for s in d.all("/api/epg/sources/"):
        print("EPG", s["id"], s["name"], s.get("status"), s.get("epg_data_count"), (s.get("last_message") or "")[:80])
    print("channels:", len(d.all("/api/channels/channels/")))
