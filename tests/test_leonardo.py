"""Importing the images he already made in Leonardo.Ai.

"I'd rather not go through and individually download the hundreds of images that I've
made." So the properties that matter are: it never generates anything (that would spend
his credits while claiming to import), one bad image does not end an import of hundreds,
and running it twice does not duplicate the lot.
"""
import json

import pytest

from assistant.core import db as core_db, design_assets, leonardo, owner_requests


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "leo.db")
    core_db.init_db(p)
    design_assets.init_design_assets(p)
    # Asking without a key now files the ask on his board, so it needs somewhere to go.
    owner_requests.init_owner_requests(p)
    return p


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        if not isinstance(self._payload, dict):
            raise ValueError("not json")
        return self._payload


def _client(monkeypatch, routes):
    """routes: {path_fragment: FakeResponse}. Records every URL it was asked for."""
    import httpx

    asked = []

    def fake_get(url, params=None, headers=None, timeout=None):
        asked.append(url)
        for fragment, response in routes.items():
            if fragment in url:
                return response
        return FakeResponse({}, status=404)

    monkeypatch.setattr(httpx, "get", fake_get)
    client = leonardo.LeonardoClient("test-key")
    client.asked = asked
    return client


ME = {"user_details": [{"user": {"id": "user-123", "username": "jack"}}]}


def _generation(n, images=2):
    return {"id": f"gen-{n}", "prompt": f"a skull in a top hat {n}",
            "createdAt": "2026-09-01T00:00:00Z",
            "generated_images": [{"id": f"img-{n}-{i}",
                                  "url": f"https://cdn.leonardo/{n}-{i}.jpg"}
                                 for i in range(images)]}


class TestItNeverGenerates:
    def test_every_call_is_a_read(self, monkeypatch, path, tmp_path):
        """A module that could spend his credits while 'importing' would be a bad trade
        for a convenience."""
        client = _client(monkeypatch, {"/me": FakeResponse(ME),
                                       "/generations/user/": FakeResponse(
                                           {"generations": [_generation(1)]})})
        leonardo.import_generations(path, 1, client, str(tmp_path / "media"),
                                    download=lambda url, target: open(target, "wb").write(b"x"))
        assert client.asked, "it did call something"
        assert not any("createGeneration" in u or "POST" in u for u in client.asked)


class TestTheProbe:
    def test_it_answers_the_undocumented_question(self, monkeypatch):
        """Leonardo does not document whether an API key sees web-app work. Asking the
        account directly settles it in seconds."""
        client = _client(monkeypatch, {"/me": FakeResponse(ME),
                                       "/generations/user/": FakeResponse(
                                           {"generations": [_generation(1), _generation(2)]})})
        out = leonardo.probe(client)
        assert out["ok"] is True and out["generations"] == 2 and out["images"] == 4
        assert "skull" in out["sample_prompt"]

    def test_a_key_with_no_api_plan_says_so_plainly(self, monkeypatch):
        client = _client(monkeypatch, {"/me": FakeResponse({}, status=403)})
        out = leonardo.probe(client)
        assert out["ok"] is False and "API plan" in out["error"]

    def test_a_bad_key_says_so_plainly(self, monkeypatch):
        client = _client(monkeypatch, {"/me": FakeResponse({}, status=401)})
        assert "401" in leonardo.probe(client)["error"]

    def test_an_empty_account_is_not_an_error(self, monkeypatch):
        client = _client(monkeypatch, {"/me": FakeResponse(ME),
                                       "/generations/user/": FakeResponse({"generations": []})})
        out = leonardo.probe(client)
        assert out["ok"] is True and out["images"] == 0


class TestImporting:
    def _ok(self, monkeypatch, pages):
        state = {"n": 0}

        def routed(url, params=None, headers=None, timeout=None):
            if "/me" in url:
                return FakeResponse(ME)
            page = pages[state["n"]] if state["n"] < len(pages) else []
            state["n"] += 1
            return FakeResponse({"generations": page})

        import httpx
        monkeypatch.setattr(httpx, "get", routed)
        return leonardo.LeonardoClient("k")

    def test_images_land_in_the_catalogue(self, monkeypatch, path, tmp_path):
        client = self._ok(monkeypatch, [[_generation(1), _generation(2)], []])
        out = leonardo.import_generations(
            path, 1, client, str(tmp_path / "media"),
            download=lambda url, target: open(target, "wb").write(b"x"))
        assert out["imported"] == 4 and out["failed"] == 0
        assert len(design_assets.catalogue(path, 1)) == 4

    def test_the_prompt_becomes_the_title(self, monkeypatch, path, tmp_path):
        """He will remember what he asked for, not a UUID."""
        client = self._ok(monkeypatch, [[_generation(1)], []])
        leonardo.import_generations(path, 1, client, str(tmp_path / "media"),
                                    download=lambda u, t: open(t, "wb").write(b"x"))
        assert "skull in a top hat" in design_assets.catalogue(path, 1)[0]["title"]

    def test_running_it_twice_does_not_duplicate_hundreds(self, monkeypatch, path,
                                                          tmp_path):
        for _ in range(2):
            client = self._ok(monkeypatch, [[_generation(1), _generation(2)], []])
            out = leonardo.import_generations(
                path, 1, client, str(tmp_path / "media"),
                download=lambda u, t: open(t, "wb").write(b"x"))
        assert out["imported"] == 0 and out["skipped"] == 4
        assert len(design_assets.catalogue(path, 1)) == 4

    def test_one_bad_download_does_not_end_the_import(self, monkeypatch, path, tmp_path):
        """Losing image 3 of 400 must not lose images 4 through 400."""
        seen = {"n": 0}

        def flaky(url, target):
            seen["n"] += 1
            if seen["n"] == 2:
                raise OSError("connection reset")
            open(target, "wb").write(b"x")

        client = self._ok(monkeypatch, [[_generation(1), _generation(2)], []])
        out = leonardo.import_generations(path, 1, client, str(tmp_path / "media"),
                                          download=flaky)
        assert out["imported"] == 3 and out["failed"] == 1

    def test_the_cap_is_respected(self, monkeypatch, path, tmp_path):
        client = self._ok(monkeypatch, [[_generation(i) for i in range(5)], []])
        out = leonardo.import_generations(path, 1, client, str(tmp_path / "media"),
                                          max_images=3,
                                          download=lambda u, t: open(t, "wb").write(b"x"))
        assert out["imported"] == 3

    def test_a_generation_with_no_images_is_skipped_quietly(self, monkeypatch, path,
                                                            tmp_path):
        empty = {"id": "g", "prompt": "p", "generated_images": []}
        client = self._ok(monkeypatch, [[empty], []])
        out = leonardo.import_generations(path, 1, client, str(tmp_path / "media"),
                                          download=lambda u, t: None)
        assert out["ok"] is True and out["imported"] == 0


class TestTheKeyHeHasNotGivenYet:
    """What happens before there is a key matters more than it looks: this sat built and
    unused for a day because the ask lived in one chat message rather than on the board
    where his blockers live."""

    def _client(self, db):
        from assistant.core import personal_tools

        return personal_tools.PersonalClient(db, 1)

    def test_it_raises_the_ask_on_his_board(self, path):
        from assistant.core import owner_requests

        self._client(path).call_tool("import_leonardo_art", {"check_only": True})
        asks = [r for r in owner_requests.list_requests(path, 1, status="open")
                if r["name"] == "leonardo_api_key"]
        assert len(asks) == 1

    def test_asking_twice_does_not_grow_the_board(self, path):
        from assistant.core import owner_requests

        client = self._client(path)
        for _ in range(3):
            client.call_tool("import_leonardo_art", {"check_only": True})
        asks = [r for r in owner_requests.list_requests(path, 1, status="open")
                if r["name"] == "leonardo_api_key"]
        assert len(asks) == 1, "a board that grows a row per run is one he stops reading"

    def test_the_ask_says_it_costs_nothing(self, path):
        """The obvious reason not to bother: 'another paid API'. It is not — credits are
        spent generating, not listing."""
        from assistant.core import owner_requests

        self._client(path).call_tool("import_leonardo_art", {"check_only": True})
        ask = [r for r in owner_requests.list_requests(path, 1, status="open")
               if r["name"] == "leonardo_api_key"][0]
        assert "cost" in (ask["instructions"] or "").lower()

    def test_a_key_pasted_into_the_board_is_used(self, path, monkeypatch):
        """He should be able to paste it into a screen rather than edit config.json, a
        file he has never opened."""
        from assistant.core import owner_requests, personal_tools

        ask = owner_requests.raise_request(path, 1, title="k", kind="secret",
                                           name="leonardo_api_key")
        owner_requests.provide(path, 1, ask, "board-key")

        seen = {}

        class Fake:
            def __init__(self, key):
                seen["key"] = key

        monkeypatch.setattr(leonardo, "LeonardoClient", Fake)
        monkeypatch.setattr(leonardo, "probe", lambda c: {"ok": True})
        personal_tools.PersonalClient(path, 1, leonardo_api_key="config-key").call_tool(
            "import_leonardo_art", {"check_only": True})
        assert seen["key"] == "board-key", "the board wins over config"
