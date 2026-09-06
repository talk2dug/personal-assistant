"""Client for LetterStream's physical mail API: postcards, first-class letters, USPS
certified mail, and flats, printed and mailed on the owner's account.

This is a real API client (like mail_client.py, home_assistant_client.py), not an MCP
server â€” LetterStream has no MCP wrapper, and the raw API is simple enough not to need
one: authenticate with an HMAC-style hash, POST a job, get XML/JSON back.

The account is prepaid ("required to maintain funds on account to cover the cost of
your submitted mailings") and every successful job costs real money and puts a real
piece of mail in the actual USPS system â€” walking a letter back once it's mailed isn't
possible. LetterStream's own API is built around exactly this risk with a native
two-step release:

    preauth=1  ->  priced, queued, but NOT sent and NOT charged
    doauth=<authcode from the preauth response>  ->  actually released to production

send_mail() always submits with preauth=1. Nothing is ever mailed by this client without
a second, explicit call to authorize() with the code that call returns â€” which is exactly
the seam engine.py's confirmation gate hooks into: send_mail (the quote) runs freely,
authorize (the release) is the one sensitive tool.
"""
import base64
import hashlib
import re
import time

import httpx

API_URL = "https://www.letterstream.com/apis/index.php"

# Values LetterStream's docs use for MailType; anything else is rejected here rather than
# sent through and rejected less clearly by their system.
MAIL_TYPES = {"firstclass", "firstclass_hse", "certified", "certnoerr", "postcard", "flat", "propostcard"}

# Confirmed against the live API (error -900), not documented in the API PDF: job names
# must be 8-20 chars of a-zA-Z0-9_-. Validated here so a bad name fails locally with a
# clear message instead of a round trip to find out.
JOB_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{8,20}$")


class LetterStreamError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"LetterStream error {code}: {message}")


def _unique_id() -> str:
    """A numeric id LetterStream will only accept once, ever.

    Unix seconds (their own docs' example) collides if two requests land in the same
    second â€” plausible for a status check right after a submission. Milliseconds keeps
    the same 10-18 digit numeric requirement (13 digits until the year 10889) with far
    finer resolution.
    """
    return str(int(time.time() * 1000))


def _hash(api_key: str, unique_id: str) -> str:
    """md5(base64(last-6-of-unique-id + api_key + first-6-of-unique-id)) â€” LetterStream's
    documented scheme. The key itself is never transmitted, only this per-request hash."""
    string_to_hash = unique_id[-6:] + api_key + unique_id[:6]
    encoded = base64.b64encode(string_to_hash.encode("utf-8"))
    return hashlib.md5(encoded).hexdigest()


def _format_address(name_1: str, name_2: str, addr_1: str, addr_2: str,
                    city: str, state: str, zip_code: str, doc_id: str | None = None) -> str:
    """LetterStream's colon/pipe-delimited address format. Pipe is used here (their docs
    allow either) so a legitimate colon in an address (e.g. a suite label) can't be
    silently mistaken for a field boundary â€” the same reasoning applies to '|', so any
    field containing one is rejected rather than corrupting the parse silently.
    """
    parts = [name_1, name_2 or "", addr_1, addr_2 or "", city, state, zip_code]
    if doc_id is not None:
        parts = [doc_id] + parts
    for p in parts:
        if "|" in p:
            raise ValueError(f"address field contains '|', LetterStream's delimiter: {p!r}")
    return "|".join(parts)


class LetterStreamClient:
    def __init__(self, api_id: str, api_key: str, timeout: float = 60.0):
        self.api_id = api_id
        self.api_key = api_key
        self.timeout = timeout

    def _auth_fields(self) -> dict:
        unique_id = _unique_id()
        return {"a": self.api_id, "h": _hash(self.api_key, unique_id), "t": unique_id}

    @staticmethod
    def _parse(resp: httpx.Response) -> dict:
        resp.raise_for_status()
        data = resp.json()
        message = (data.get("messages") or {}).get("message") or data.get("message") or data
        if isinstance(message, list):
            message = message[0]
        code = int(message.get("code", 0))
        # -100 and -200 are LetterStream's success families (full send, preauth/doauth);
        # every other negative code is documented as an error (e.g. -911 low balance).
        if code < 0 and code not in (-100, -200):
            raise LetterStreamError(code, message.get("details", "unknown error"))
        return message

    def _post(self, fields: dict, files: dict | None = None) -> dict:
        form = {**self._auth_fields(), **fields, "responseformat": "json"}
        resp = httpx.post(API_URL, data=form, files=files, timeout=self.timeout)
        return self._parse(resp)

    def send_mail(self, job: str, to: list[dict], from_addr: dict, pdf_bytes: bytes,
                  pages: int, mailtype: str = "firstclass", coversheet: bool = True,
                  duplex: bool = False, ink: str = "B", paper: str = "W",
                  return_envelope: str = "N") -> dict:
        """Quote a mailing. Always submits preauth=1 -- see the module docstring for why
        this is the only entry point that reaches LetterStream's production queue, and
        why it is still safe to call without a confirmation gate: nothing is sent or
        charged until authorize() is called with the authcode this returns.

        to: list of {"name_1", "name_2", "addr_1", "addr_2", "city", "state", "zip",
        "doc_id"} (name_2/addr_2/doc_id optional; doc_id defaults to job-<index>).
        from_addr: same shape, no doc_id.
        """
        if mailtype not in MAIL_TYPES:
            raise ValueError(f"mailtype must be one of {sorted(MAIL_TYPES)}, got {mailtype!r}")
        if not to:
            raise ValueError("at least one recipient is required")
        if not JOB_NAME_RE.match(job):
            raise ValueError(f"job name must be 8-20 characters of a-zA-Z0-9_- , got {job!r}")

        fields = {
            "job": job, "pages": str(pages), "mailtype": mailtype,
            "coversheet": "Y" if coversheet else "N", "duplex": "Y" if duplex else "N",
            "ink": ink, "paper": paper, "returnenv": return_envelope, "preauth": "1",
        }
        fields["from"] = _format_address(
            from_addr["name_1"], from_addr.get("name_2", ""), from_addr["addr_1"],
            from_addr.get("addr_2", ""), from_addr["city"], from_addr["state"], from_addr["zip"])
        to_strings = []
        for i, r in enumerate(to):
            doc_id = r.get("doc_id") or f"{job}-{i}"
            to_strings.append(_format_address(
                r["name_1"], r.get("name_2", ""), r["addr_1"], r.get("addr_2", ""),
                r["city"], r["state"], r["zip"], doc_id=doc_id))

        # httpx expands a list-valued dict entry into repeated multipart fields with the
        # same name -- the way to send to[] once per recipient under one form.
        fields["to[]"] = to_strings
        files = {"single_file": (f"{job}.pdf", pdf_bytes, "application/pdf")}
        return self._post(fields, files=files)

    def authorize(self, authcode: str) -> dict:
        """Releases a preauth'd job into production: real postage, real cost, a real
        piece of mail that cannot be recalled once accepted. This is the one call in
        this client that engine.py must gate behind an explicit user confirmation."""
        return self._post({"doauth": authcode})

    def track(self, cert: str | None = None, doc_id: str | None = None, kind: str = "track") -> dict:
        """kind: 'track' (status/tracking), 'sig' (signature file, certified only),
        or 'proof' (base64 PDF proof, by doc_id only)."""
        if not cert and not doc_id:
            raise ValueError("cert or doc_id is required")
        fields = {"getinfo": kind}
        if cert:
            fields["cert"] = cert
        else:
            fields["doc_id"] = doc_id
        return self._post(fields)

    def batch_status(self, *batch_ids: str) -> dict:
        return self._post({"batchstatus": ",".join(batch_ids)})

    def job_status(self, *job_ids: str) -> dict:
        return self._post({"jobstatus": ",".join(job_ids)})

    def doc_status(self, *doc_ids: str) -> dict:
        return self._post({"docstatus": ",".join(doc_ids)})

    def account_status(self) -> dict:
        """Prepaid balance information -- read-only, safe to call freely."""
        return self._post({"accountstatus": "1"})


class LetterStreamTools:
    """Adapts LetterStreamClient to the call_tool(name, arguments) shape every
    tool-bearing engine.py context exposes (same pattern as MailClient.call_tool) â€” so
    _dispatch_tool_call and the confirmation gate don't need anything LetterStream-
    specific beyond the sensitive_tools set.

    from_addr is fixed at construction (the owner's own return address, from config)
    rather than an argument the model supplies on every call: it does not change per
    letter, and asking the model to restate it each time is one more place a typo could
    put the wrong return address on real outgoing mail.
    """

    def __init__(self, client: LetterStreamClient, from_addr: dict):
        self.client = client
        self.from_addr = from_addr

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name == "letterstream_send_mail":
            from .letter_pdf import text_to_pdf

            pdf_bytes, pages = text_to_pdf(arguments["letter_text"])
            job = f"jrv{int(time.time()) % 10_000_000_000:010d}"
            recipient = {
                "name_1": arguments["recipient_name"], "addr_1": arguments["recipient_address"],
                "addr_2": arguments.get("recipient_address_2", ""),
                "city": arguments["recipient_city"], "state": arguments["recipient_state"],
                "zip": arguments["recipient_zip"],
            }
            result = self.client.send_mail(
                job=job, to=[recipient], from_addr=self.from_addr,
                pdf_bytes=pdf_bytes, pages=pages,
                mailtype=arguments.get("mail_type", "firstclass"),
            )
            # doc_id is deterministic (send_mail defaults an unspecified single
            # recipient's doc_id to "{job}-0") but was never handed back to the caller
            # before -- callers that need to look this mailing back up later (tracking,
            # the credit dispute tracker) need a stable id, and re-deriving the string
            # "{job}-0" at every call site would just be this same logic copy-pasted.
            # LetterStream's own response fields win on any (unexpected) collision.
            return {"job": job, "doc_id": f"{job}-0", **result}

        if name == "letterstream_authorize_mail":
            return self.client.authorize(arguments["authcode"])

        if name == "letterstream_track_mail":
            return self.client.track(cert=arguments.get("tracking_number"),
                                     doc_id=arguments.get("doc_id"),
                                     kind=arguments.get("kind", "track"))

        if name == "letterstream_account_balance":
            return self.client.account_status()

        return {"error": f"unknown letterstream tool {name}"}
