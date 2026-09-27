# Omada network access — visibility plus one gated control action

*Built 2026-09-27. Branch `needs-you-board`.*

## What Jack asked for

Jarvis should be able to see what's actually on his TP-Link Omada-managed network
(controller at `192.168.0.100`) — devices (APs/switches/gateway), connected clients,
health — and, narrowly, act on it. Control actions must be **gated the same way SSH
ops-plans already are**: he approves each one, not a freely-callable tool a model could
invoke repeatedly.

## The shape

```
Omada controller (Open API, OAuth2 client-credentials)
        │ list_devices / list_clients / reboot_device
        ▼
omada_client.py (OmadaClient) ──── chat tools (omada_tools.py) ──── engine.py dispatch
        │                                                                 │
        │                                                    sensitive_tools gate
        │                                                    (same as git_merge_pr,
        │                                                     Kroger, CCXT, HA locks)
        ▼
omada_health.py (omada_devices / omada_clients tables)
        │
JarvisCore scheduler: omada_health_tick every 15 min ──> attention.should_say ──> notify()
```

## Decisions, and why

**Open API (OAuth2 client-credentials), not the legacy session-cookie Web API.** The
legacy API's session cookie name has already changed once across Omada firmware versions
(`TPEAP_SESSIONID` → `TPOMADA_SESSIONID`), and it needs a raw username/password. The Open
API is what TP-Link is actively investing in, is token-scoped, and needs only a Client
ID/Secret the owner generates himself on the controller's own Settings → Open API screen —
never a raw credential in config.json, same principle as every other integration here.

**`Authorization: AccessToken=<token>`, not `Bearer <token>`.** Confirmed live against a
real controller (firmware 5.14.32.56, API v3): the token response's own `tokenType` field
says "bearer", but a `Bearer` header gets a token-expired-shaped error even for a
brand-new token. `AccessToken=` is what the controller actually accepts. See
`omada_client.py`'s module docstring for the two other TP-Link-specific quirks confirmed
the same way (not assumed from vendor docs, which don't give exact request shapes).

**Control goes through `engine.py`'s `sensitive_tools` confirmation gate, not
`ops_plans.py`.** `ops_plans.py`/`ssh_ops.py` is a multi-step change/test/verify/rollback
executor built for scripted server changes over SSH — forcing "reboot this switch" through
it would mean faking the action as an SSH command. The `sensitive_tools` pattern already
used for `git_merge_pr`, Home Assistant's lock/cover/alarm domains, Kroger checkout, and
CCXT trades is sized correctly for a single atomic action approved once: `engine.py`
appends a `pending_actions` row and returns `awaiting_confirmation`; only an explicit yes
from the owner (in chat or on the Review page) calls the real `OmadaClient.call_tool`.

**Only `reboot_omada_device` ships as a control action, deliberately.** A reboot
self-heals; a bad `block_client` call against the wrong device could cut Jack's own access
to something. Same "ship narrow, expand deliberately" discipline as `host_fixes.py`'s
fix whitelist starting empty — a wider control surface (blocking a client) waits until the
monitoring half has run for a while and the pattern's proven.

**`omada_health.py` mirrors `host_health.py` exactly for devices** (a small, known, fixed
set — `check_devices`/`record_device_check` return only what transitioned: down,
recovered, still down) **but adds a second, genuinely new shape for clients**: an OPEN set
that grows as new devices join the network. `check_and_record_clients` reports a client
seen for the very first time as its own kind of event — the same "own vs. unknown-new"
idea `docs/radio-awareness-design.md` uses for RF-baseline vehicle classification, applied
here to who's on the wifi.

**No auto-remediation for network devices**, unlike `host_fixes.py`'s SSH-reachable
whitelist. A switch or AP that won't come back on its own needs the owner, not a guessed
fix — `run_omada_health_tick` only ever measures and notifies; `reboot_omada_device`
already requires his explicit confirmation regardless.

**Read tools are always offered, not keyword-gated**, same reasoning as the git tools:
"is the network okay" and "who just joined the wifi" name no reliable keyword, and it's
only 4 schemas — nowhere near the tool-count-overload budget that forced Era/Kroger to
split their catalogs.

## Config

`omada_controller_url`, `omada_client_id`, `omada_client_secret`, `omada_id` (the Open
API's own per-controller namespace, obtained via the controller's unauthenticated
`GET /api/info` if not given directly), `omada_site_id` — all `str | None`, all sourced
from the owner, mirroring every other credential in `config.json`.

## Verification

Confirmed live against the real controller before shipping: `list_devices()` /
`list_clients()` returned the real 3 devices (router/ER605, two EAPs) and 48 real clients.
`reboot_omada_device` was proven to hit the confirmation gate and stop there — it was
never called against a real device during this build (see `tests/test_engine_omada.py`'s
`test_reboot_creates_a_pending_action_not_a_real_reboot`).
