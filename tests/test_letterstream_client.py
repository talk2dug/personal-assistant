"""Unit tests for the LetterStream API client: the auth hash, address formatting, job
name validation, and response handling. All HTTP is mocked -- these must never make a
real network call, since a bug here plus a real call is how a test accidentally mails
something.
"""
import base64
import hashlib

import httpx
import pytest

from assistant.core.letterstream_client import (
    LetterStreamClient, LetterStreamError, LetterStreamTools, _format_address, _hash, _unique_id,
)


def test_hash_matches_the_documented_algorithm():
    # From LetterStream's own API doc example, reproduced exactly.
    api_key = "testkey123"
    unique_id = "1234567890123"
    expected = hashlib.md5(
        base64.b64encode((unique_id[-6:] + api_key + unique_id[:6]).encode())
    ).hexdigest()
    assert _hash(api_key, unique_id) == expected


def test_unique_id_is_numeric_and_in_the_documented_length_range():
    uid = _unique_id()
    assert uid.isdigit()
    assert 10 <= len(uid) <= 18


def test_format_address_joins_with_pipe():
    addr = _format_address("John", "Doe", "123 S Sunny St", "Suite 101", "Scottsdale", "AZ", "85281")
    assert addr == "John|Doe|123 S Sunny St|Suite 101|Scottsdale|AZ|85281"


def test_format_address_includes_doc_id_when_given():
    addr = _format_address("John", "", "123 S Sunny St", "", "Scottsdale", "AZ", "85281", doc_id="abc123")
    assert addr.startswith("abc123|")


def test_format_address_rejects_a_delimiter_inside_a_field():
    """A stray '|' in a field would silently shift every field after it -- e.g. an
    apartment number ending up in the city column -- so this must fail loudly instead."""
    with pytest.raises(ValueError, match=r"\|"):
        _format_address("John", "", "123 Main St | Rear", "", "Scottsdale", "AZ", "85281")


class FakeTransport(httpx.BaseTransport):
    """Returns a fixed JSON body regardless of the request, and records what was sent."""

    def __init__(self, json_body: dict, status_code: int = 200):
        self.json_body = json_body
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json=self.json_body)


@pytest.fixture
def client(monkeypatch):
    return LetterStreamClient(api_id="testid12", api_key="testkey123")


def _patch_httpx(monkeypatch, json_body, status_code=200):
    transport = FakeTransport(json_body, status_code)

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kwargs)

    monkeypatch.setattr("assistant.core.letterstream_client.httpx.post", fake_post)
    return transport


def test_account_status_parses_a_successful_response(client, monkeypatch):
    _patch_httpx(monkeypatch, {"balance": "42.50", "id": "testid12", "testmode": "enabled"})
    result = client.account_status()
    assert result["balance"] == "42.50"


def test_a_negative_error_code_raises(client, monkeypatch):
    _patch_httpx(monkeypatch, {"code": "-911", "details": "Insufficient funding"})
    with pytest.raises(LetterStreamError, match="Insufficient funding"):
        client.account_status()


def test_preauth_success_code_does_not_raise(client, monkeypatch):
    """-200 is LetterStream's preauth success family, not an error, even though it's
    negative like every error code -- this is the exact code a real quote returns."""
    _patch_httpx(monkeypatch, {"code": "-200", "details": "Successful preauth", "authcode": "abc"})
    result = client.account_status()
    assert result["authcode"] == "abc"


def test_send_mail_rejects_a_bad_job_name(client):
    with pytest.raises(ValueError, match="8-20 characters"):
        client.send_mail(
            job="short", to=[{"name_1": "A", "addr_1": "1 St", "city": "X", "state": "AZ", "zip": "85281"}],
            from_addr={"name_1": "B", "addr_1": "2 St", "city": "Y", "state": "AZ", "zip": "85281"},
            pdf_bytes=b"%PDF-fake", pages=1,
        )


def test_send_mail_rejects_an_unknown_mailtype(client):
    with pytest.raises(ValueError, match="mailtype"):
        client.send_mail(
            job="validjobname1234", mailtype="carrier_pigeon",
            to=[{"name_1": "A", "addr_1": "1 St", "city": "X", "state": "AZ", "zip": "85281"}],
            from_addr={"name_1": "B", "addr_1": "2 St", "city": "Y", "state": "AZ", "zip": "85281"},
            pdf_bytes=b"%PDF-fake", pages=1,
        )


def test_send_mail_requires_at_least_one_recipient(client):
    with pytest.raises(ValueError, match="recipient"):
        client.send_mail(
            job="validjobname1234", to=[],
            from_addr={"name_1": "B", "addr_1": "2 St", "city": "Y", "state": "AZ", "zip": "85281"},
            pdf_bytes=b"%PDF-fake", pages=1,
        )


def test_send_mail_always_sets_preauth(client, monkeypatch):
    """send_mail must never be able to release a job into production by itself -- that
    is the property the whole confirmation gate depends on."""
    transport = _patch_httpx(monkeypatch, {"code": "-200", "details": "Successful preauth",
                                           "authcode": "xyz", "cost": "1.19"})
    client.send_mail(
        job="validjobname1234",
        to=[{"name_1": "A", "addr_1": "1 St", "city": "X", "state": "AZ", "zip": "85281"}],
        from_addr={"name_1": "B", "addr_1": "2 St", "city": "Y", "state": "AZ", "zip": "85281"},
        pdf_bytes=b"%PDF-fake", pages=1,
    )
    sent = transport.requests[0]
    body = sent.read().decode()
    assert "preauth" in body


def test_authorize_sends_doauth(client, monkeypatch):
    transport = _patch_httpx(monkeypatch, {"code": "-200", "details": "Success"})
    client.authorize("some-authcode")
    body = transport.requests[0].read().decode()
    assert "doauth" in body and "some-authcode" in body


class TestLetterStreamTools:
    def test_call_tool_uses_the_configured_from_address_not_a_model_supplied_one(self, monkeypatch):
        """from_addr is fixed at construction time; call_tool's arguments never include
        it at all, so there is no argument path by which a model could put the wrong
        return address on a real letter."""
        calls = []

        class FakeClient:
            def send_mail(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True}

        tools = LetterStreamTools(FakeClient(), from_addr={"name_1": "Real", "addr_1": "1 Real St",
                                                            "city": "Real City", "state": "VA", "zip": "23219"})
        tools.call_tool("letterstream_send_mail", {
            "letter_text": "hello", "recipient_name": "Bob", "recipient_address": "1 Bob St",
            "recipient_city": "Bobtown", "recipient_state": "AZ", "recipient_zip": "85281",
        })
        assert calls[0]["from_addr"]["name_1"] == "Real"

    def test_unknown_tool_name_returns_an_error_not_an_exception(self):
        tools = LetterStreamTools(client=None, from_addr={})
        result = tools.call_tool("letterstream_bogus_tool", {})
        assert "error" in result
