"""Shared startup wiring for anything that needs Era/Calendar context — used by both
main.py (Telegram) and web_main.py (the web UI), so the two entrypoints don't duplicate
this logic.
"""
import logging
import os
import shutil
from pathlib import Path

from . import business_db, db, gpu_bridge, kitchen_db, market_data, ops_plans, paper_trading, personal_db, staff
from .business_tools import BusinessClient
from .caldav_client import CalDAVClient
from .comfy_client import ComfyClient
from .claude_cli import ClaudeCLIClient
from .engine import (
    AirbnbContext, BusinessContext, CalendarContext, CCXTContext, EraContext, GitOpsContext,
    HomeAssistantContext, KrogerContext, LetterStreamContext, MailContext, ObsidianContext,
    PersonalContext, PhoneContext, RecipeContext, TicketmasterContext,
)
from .git_ops import GitOpsClient
from .git_tools import GIT_TOOLS
from .personal_tools import PersonalClient
from .home_assistant_client import HomeAssistantClient
from .kroger_recipe import RECIPE_TOOL_SCHEMA, KrogerRecipeClient
from .mail_client import MailClient
from .mcp_client import MCPClient
from .mcp_stdio_client import StdioMCPClient
from .obsidian_client import ObsidianClient
from .ssh_ops import SSHOpsClient

logger = logging.getLogger(__name__)


def build_llm(cfg, owner_user_id: int | None = None):
    """Picks the LLM backend from config so all three entrypoints agree.

    owner_user_id is only meaningful for the Claude CLI backend: its tool calls arrive
    at /api/tools/call from a subprocess with no session cookie, so the user they
    execute as has to be established up front rather than inferred per request.
    """
    if cfg.llm_backend == "claude_cli":
        client = ClaudeCLIClient(
            cli_path=cfg.claude_cli_path, model=cfg.claude_model, tools_url=cfg.claude_tools_url,
            tools_token=cfg.claude_tools_api_key, user_id=owner_user_id,
            timeout=cfg.claude_timeout_seconds,
        )
        logger.info("LLM backend: Claude CLI (%s) at %s", cfg.claude_model, client.cli_path)
        return client

    from .llm import LLMClient

    logger.info("LLM backend: Ollama (%s) at %s", cfg.ollama_model, cfg.ollama_host)
    return LLMClient(cfg.ollama_host, cfg.ollama_model)


def build_notifier(cfg, telegram_notify, home_assistant=None):
    """Wraps the Telegram notifier so Jarvis's own outgoing messages follow the user's
    notification policy.

    This is what makes "send your notifications to my phone when I'm out" actually mean
    something. Without it the policy would only govern notifications Jarvis chose to send
    during a conversation, while reminders — the ones that matter most when you're not at
    a screen — would keep going to Telegram regardless.

    Policy lives in the settings table and is read per send, not cached, so changing it in
    chat takes effect on the next reminder rather than the next restart.
    """
    from .engine import DEFAULT_NOTIFY_POLICY, NOTIFY_POLICY_KEY

    def notify(chat_id, text):
        policy = db.get_setting(cfg.db_path, NOTIFY_POLICY_KEY, DEFAULT_NOTIFY_POLICY)
        to_phone = False

        if home_assistant is not None:
            if policy in ("phone", "both"):
                to_phone = True
            elif policy == "auto":
                try:
                    presence = home_assistant.mcp_client.presence()
                    # Only push when we actually know he's out. Unknown presence falls
                    # back to Telegram rather than pushing to a phone in his pocket at
                    # 3am on a guess.
                    to_phone = presence.get("home") is False
                except Exception:
                    logger.debug("presence lookup failed; falling back to Telegram")

        if to_phone:
            try:
                result = home_assistant.mcp_client.notify(text, title="Jarvis")
                if not result.get("error"):
                    if policy != "both":
                        return
                else:
                    logger.warning("phone notify failed (%s) — falling back to Telegram", result["error"])
            except Exception:
                logger.exception("phone notify failed — falling back to Telegram")

        telegram_notify(chat_id, text)

    return notify


def build_gpu_bridge(cfg):
    """simrig is a real machine that gets gamed on, rebooted and unplugged. An
    unreachable bridge must only disable offloading, never take down the assistant —
    same defensive posture as the phone and Home Assistant contexts. It's still returned
    when unreachable, so the queue accepts work and drains when the box comes back."""
    if not cfg.gpu_bridge_enabled or not cfg.gpu_bridge_host:
        return None
    gpu_bridge.init_bridge_db(cfg.db_path)
    comfy = ComfyClient(cfg.comfy_host) if cfg.comfy_host else None
    bridge = gpu_bridge.GPUBridge(
        cfg.db_path, cfg.gpu_bridge_host, task_models=cfg.gpu_task_models,
        max_concurrent=cfg.gpu_max_concurrent, comfy=comfy, media_dir=cfg.generated_media_path,
    )
    if comfy is not None:
        logger.info("ComfyUI: %s %s", cfg.comfy_host,
                    "reachable" if comfy.reachable() else "NOT reachable (image/video will queue)")
    if bridge.reachable():
        installed = {m["model"] for m in bridge.installed_models()}
        missing = [
            route["model"] for route in bridge.task_models.values()
            if route.get("engine") == "ollama" and route.get("model") and route["model"] not in installed
        ]
        logger.info(
            "GPU bridge: %s reachable, mode=%s, %d models installed%s",
            cfg.gpu_bridge_host, bridge.get_mode()["mode"], len(installed),
            f", MISSING: {missing}" if missing else "",
        )
    else:
        logger.warning("GPU bridge: %s unreachable at startup — jobs will queue until it returns",
                       cfg.gpu_bridge_host)
    return bridge


def build_business_context(cfg, owner_user_id: int | None, llm=None, bridge=None) -> BusinessContext | None:
    """The business side is owner-only and entirely local, so unlike Era/mail/HA there's
    no network dependency to fail at startup — if a profile is configured and we know who
    the owner is, it works."""
    if not cfg.business or owner_user_id is None:
        return None
    business_db.init_business_db(cfg.db_path)
    staff.init_staff_db(cfg.db_path)
    market_data.init_market_db(cfg.db_path)
    paper_trading.init_paper_db(cfg.db_path)
    ops_plans.init_ops_plans_db(cfg.db_path)
    # ssh_hosts defaults to {} (no hosts registered) rather than gating on a whole
    # separate enabled flag -- propose_ops_plan already refuses any step targeting an
    # unregistered host, so an empty registry is already a safe, self-explaining no-op.
    ssh_ops = SSHOpsClient(cfg.ssh_hosts) if cfg.ssh_hosts else None
    client = BusinessClient(cfg.db_path, owner_user_id, llm=llm, profile=cfg.business, bridge=bridge, ssh_ops=ssh_ops)
    scheduled = cfg.business_agents_enabled and hasattr(llm, "research")
    logger.info(
        "Business: %s (%s), agents %s", cfg.business.name, cfg.business.location,
        "scheduled" if scheduled
        else ("on-demand only" if hasattr(llm, "research") else "unavailable on this LLM backend"),
    )
    if ssh_ops is not None:
        logger.info("Ops plans: %d SSH host(s) registered (%s)", len(cfg.ssh_hosts), ", ".join(sorted(cfg.ssh_hosts)))
    return BusinessContext(
        mcp_client=client, profile=cfg.business, agents_scheduled=scheduled,
        has_gpu_bridge=bridge is not None,
    )


# Merging to main is the only git tool with real consequence -- see GitOpsContext's
# docstring. Branch/write/push/PR-open (including reading the repo) are all reversible
# and execute immediately.
GIT_SENSITIVE_TOOLS = {"git_merge_pr"}


def build_git_ops_context(cfg) -> GitOpsContext | None:
    """Dev-team git tools: off unless both a target repo and a PAT are configured."""
    if not cfg.github_repo or not cfg.github_pat:
        return None
    client = GitOpsClient(
        cfg.github_repo, cfg.github_pat, cfg.git_workspace_path,
        author_name=cfg.git_author_name, author_email=cfg.git_author_email,
    )
    logger.info("Git ops: targeting %s, %d tools, %d gated as sensitive",
               cfg.github_repo, len(GIT_TOOLS), len(GIT_SENSITIVE_TOOLS))
    return GitOpsContext(mcp_client=client, git_tools=GIT_TOOLS, sensitive_tools=GIT_SENSITIVE_TOOLS)


def build_personal_context(cfg, owner_user_id: int | None, letterstream: LetterStreamContext | None = None) -> PersonalContext | None:
    """The owner's own projects/tasks/errands/pantry/credit tracking — core owner data,
    not an opt-in feature like the business profile, so the only real gate is knowing who
    the owner is.

    letterstream is optional and, when given, is unwrapped to its .mcp_client
    (LetterStreamTools) before being handed to PersonalClient -- see personal_tools.py's
    docstring for why draft_dispute_letter/track_dispute_letter reach out to it directly
    rather than duplicating any of LetterStream's own auth/PDF/preauth logic. A
    deployment with no LetterStream configured still gets every other personal tool;
    those two just degrade to a clear "not configured" error.
    """
    if owner_user_id is None:
        return None
    personal_db.init_personal_db(cfg.db_path)
    kitchen_db.init_kitchen_db(cfg.db_path)
    # One-time (idempotent) move off the old have/low/out pantry board onto real
    # quantities -- see kitchen_inventory's schema comment in kitchen_db.py. Cheap to
    # call every boot: it's a no-op once pantry_items is empty.
    migrated = kitchen_db.migrate_pantry_to_inventory(cfg.db_path, owner_user_id)
    if migrated:
        logger.info("Kitchen: migrated %d pantry item(s) to kitchen_inventory with placeholder quantities: %s",
                    len(migrated), ", ".join(migrated))
    letterstream_tools = letterstream.mcp_client if letterstream is not None else None
    return PersonalContext(mcp_client=PersonalClient(cfg.db_path, owner_user_id, letterstream=letterstream_tools))


def build_era_context(cfg) -> EraContext | None:
    if not cfg.era_api_key:
        return None
    mcp_client = MCPClient(cfg.era_mcp_url, cfg.era_api_key)
    discovered = mcp_client.list_tools()
    era_tools = [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in discovered
    ]
    logger.info("Era: discovered %d tools, %d gated as sensitive", len(era_tools), len(cfg.era_sensitive_tools))
    return EraContext(mcp_client=mcp_client, era_tools=era_tools, sensitive_tools=set(cfg.era_sensitive_tools))


def build_recipe_context(cfg) -> RecipeContext | None:
    if not cfg.recipe_api_key:
        return None
    mcp_client = MCPClient(cfg.recipe_mcp_url, cfg.recipe_api_key)
    discovered = mcp_client.list_tools()
    recipe_tools = [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in discovered
    ]
    logger.info("Recipe API: discovered %d tools", len(recipe_tools))
    return RecipeContext(mcp_client=mcp_client, recipe_tools=recipe_tools)


def build_phone_context(cfg) -> PhoneContext | None:
    """Unlike Era/CalDAV (stable cloud APIs), the phone MCP server runs on an Android
    phone that can be asleep, backgrounded, off wifi, or mid-reboot at any given moment —
    far more likely to be unreachable at startup. That must only disable the phone
    feature, never take down the whole assistant (Telegram/reminders/web), so failures
    here are caught and logged rather than propagated."""
    if not cfg.phone_mcp_url:
        return None
    try:
        mcp_client = MCPClient(cfg.phone_mcp_url)
        discovered = mcp_client.list_tools()
    except Exception as e:
        logger.warning("Phone MCP server unreachable at startup (%s) — phone tools disabled this session", e)
        return None
    phone_tools = [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in discovered
    ]
    logger.info("Phone: discovered %d tools, %d gated as sensitive", len(phone_tools), len(cfg.phone_sensitive_tools))
    return PhoneContext(mcp_client=mcp_client, phone_tools=phone_tools, sensitive_tools=set(cfg.phone_sensitive_tools))


def build_mail_context(cfg) -> MailContext | None:
    """iCloud Mail shares the CalDAV Apple ID/app-specific password (Phase 3) — same
    account, different protocol. A bad password or Apple-side account change should
    only disable mail, never take down the rest of the assistant, so the startup
    connectivity check is guarded the same way build_phone_context's is."""
    if not cfg.apple_id or not cfg.apple_app_password:
        return None
    client = MailClient(cfg.apple_id, cfg.apple_app_password, junk_threshold=cfg.mail_junk_score_threshold)
    try:
        client.list_recent(limit=1)
    except Exception as e:
        logger.warning("iCloud Mail unreachable/auth failed at startup (%s) — mail tools disabled this session", e)
        return None
    logger.info(
        "Mail: iCloud IMAP/SMTP connected, %d tools gated as sensitive, junk-scan threshold %.1f",
        len(cfg.mail_sensitive_tools), cfg.mail_junk_score_threshold,
    )
    return MailContext(mcp_client=client, sensitive_tools=set(cfg.mail_sensitive_tools))


def build_obsidian_context(cfg) -> ObsidianContext | None:
    """The Obsidian vault is just a folder on disk — no auth, no network. A missing
    vault path disables it; a real filesystem error (permissions, a bad drive letter)
    should only disable the vault, never take down the rest of the assistant, same
    defensive posture as the other optional contexts."""
    if not cfg.obsidian_vault_path:
        return None
    try:
        client = ObsidianClient(cfg.obsidian_vault_path)
        client.list_notes()  # cheap sanity check that the vault path is real/readable
    except Exception as e:
        logger.warning("Obsidian vault unreachable at startup (%s) — vault tools disabled this session", e)
        return None
    logger.info("Obsidian: vault connected at %s", cfg.obsidian_vault_path)
    return ObsidianContext(mcp_client=client)


def build_home_assistant_context(cfg) -> HomeAssistantContext | None:
    """Home Assistant is a real device on the LAN that could be off/rebooting/
    unreachable at any moment — same reasoning as build_phone_context, only disable
    the smart-home tools on failure, never take down the rest of the assistant."""
    if not cfg.ha_base_url or not cfg.ha_token:
        return None
    try:
        client = HomeAssistantClient(cfg.ha_base_url, cfg.ha_token, cfg.ha_notify_target)
        client.list_entities()
    except Exception as e:
        logger.warning("Home Assistant unreachable at startup (%s) — HA tools disabled this session", e)
        return None
    logger.info(
        "Home Assistant: connected at %s, sensitive domains: %s", cfg.ha_base_url, cfg.ha_sensitive_domains
    )
    return HomeAssistantContext(mcp_client=client, sensitive_domains=set(cfg.ha_sensitive_domains))


def build_calendar_context(cfg) -> CalendarContext | None:
    if not cfg.apple_id or not cfg.apple_app_password:
        return None
    client = CalDAVClient(cfg.caldav_url, cfg.apple_id, cfg.apple_app_password)
    logger.info(
        "Apple Calendar: connected, personal=%s shared=%s", cfg.caldav_personal_calendar, cfg.caldav_shared_calendar
    )
    return CalendarContext(
        client=client, personal_calendar=cfg.caldav_personal_calendar, shared_calendar=cfg.caldav_shared_calendar
    )


def _npm_global_bin(name: str) -> str | None:
    """Resolve an npm -g installed command's executable path.

    shutil.which() first, since that's correct if npm's global bin dir is on PATH. Falls
    back to the standard per-user Windows location because JarvisCore/JarvisWeb run under
    Task Scheduler, which does not necessarily inherit the same PATH an interactive shell
    has -- confirmed missing from os.environ['PATH'] when this was built.
    """
    found = shutil.which(name)
    if found:
        return found
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "npm" / f"{name}.cmd"
        if candidate.exists():
            return str(candidate)
    return None


def _mcp_tool_schemas(discovered: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in discovered
    ]


def build_airbnb_context(cfg) -> AirbnbContext | None:
    """Airbnb search needs no credentials at all -- see AirbnbContext's docstring for
    why that makes it safe to wire in with no sensitive_tools gate. Off by default
    anyway (airbnb_mcp_enabled) so a fresh checkout doesn't spawn an extra process the
    owner never asked for."""
    if not cfg.airbnb_mcp_enabled:
        return None
    exe = _npm_global_bin("mcp-server-airbnb")
    if exe is None:
        logger.warning(
            "Airbnb MCP server not found (npm install -g @openbnb/mcp-server-airbnb) — disabled this session"
        )
        return None
    # --ignore-robots-txt: without it, Airbnb's own robots.txt blocks the one search path
    # this server hits, making it a tool that always returns "blocked". Still read-only
    # public listing data with no login either way.
    client = StdioMCPClient(exe, args=["--ignore-robots-txt"])
    try:
        tools = _mcp_tool_schemas(client.list_tools())
    except Exception as e:
        logger.warning("Airbnb MCP server failed to start (%s) — disabled this session", e)
        return None
    logger.info("Airbnb: %d tools discovered", len(tools))
    return AirbnbContext(mcp_client=client, airbnb_tools=tools)


def build_ticketmaster_context(cfg) -> TicketmasterContext | None:
    """Read-only Discovery API wrapper -- see TicketmasterContext's docstring for why
    that means no sensitive_tools gate is needed even with a real API key configured."""
    if not cfg.ticketmaster_api_key:
        return None
    exe = _npm_global_bin("mcp-server-ticketmaster")
    if exe is None:
        logger.warning(
            "Ticketmaster MCP server not found (npm install -g @delorenj/mcp-server-ticketmaster) — "
            "disabled this session"
        )
        return None
    client = StdioMCPClient(exe, env={"TICKETMASTER_API_KEY": cfg.ticketmaster_api_key})
    try:
        tools = _mcp_tool_schemas(client.list_tools())
    except Exception as e:
        logger.warning("Ticketmaster MCP server failed to start (%s) — disabled this session", e)
        return None
    logger.info("Ticketmaster: %d tools discovered", len(tools))
    return TicketmasterContext(mcp_client=client, ticketmaster_tools=tools)


# Kroger's cart/order tools are the only ones here that touch the owner's real account.
# There is no checkout tool at all -- see KrogerContext's docstring -- but adding a real
# item to a real cart is still a change outside Jarvis's own database, so it's gated the
# same conservative way mail's send_email is.
KROGER_SENSITIVE_TOOLS = {"add_items_to_cart", "bulk_add_to_cart", "mark_order_placed"}


def build_kroger_context(cfg) -> KrogerContext | None:
    if not cfg.kroger_client_id or not cfg.kroger_client_secret:
        return None
    # .venv-kroger, not the shared .venv: installing kroger-mcp into the main environment
    # downgraded the shared `mcp` SDK package in a way that broke Era/phone/Obsidian's own
    # MCP client (Tool.input_schema renamed) -- isolating it here is the fix, not a
    # preference, same reasoning as the vision stack's own separate .venv-vision.
    exe = str(Path(__file__).resolve().parents[2] / ".venv-kroger" / "Scripts" / "kroger-mcp.exe")
    if not Path(exe).exists():
        logger.warning("Kroger MCP server not found at %s (pip install kroger-mcp into "
                       ".venv-kroger) — disabled this session", exe)
        return None
    raw_client = StdioMCPClient(exe, env={
        "KROGER_CLIENT_ID": cfg.kroger_client_id,
        "KROGER_CLIENT_SECRET": cfg.kroger_client_secret,
        "KROGER_REDIRECT_URI": cfg.kroger_redirect_uri,
    })
    try:
        tools = _mcp_tool_schemas(raw_client.list_tools())
    except Exception as e:
        logger.warning("Kroger MCP server failed to start (%s) — disabled this session", e)
        return None
    # add_recipe_to_cart is synthetic (Jarvis's own, not part of kroger-mcp's catalog) --
    # see kroger_recipe.py for why matching a recipe's ingredients to real products is a
    # judgment call that belongs here rather than in the vendored server.
    tools = tools + [RECIPE_TOOL_SCHEMA]
    client = KrogerRecipeClient(raw_client)
    logger.info("Kroger: %d tools discovered, %d gated as sensitive (cart/order writes)",
               len(tools), len(KROGER_SENSITIVE_TOOLS))
    return KrogerContext(mcp_client=client, kroger_tools=tools, sensitive_tools=KROGER_SENSITIVE_TOOLS)


# Every trading-capable action this server exposes -- discovered against, not assumed:
# build_ccxt_context intersects this with the server's real tool list, so a name here
# that this deployment's version doesn't actually have is simply never gated (there's
# nothing to gate), and a real one it does have can never be missed by editing this list
# by hand from a README that turned out not to match the installed version.
CCXT_SENSITIVE_TOOL_NAMES = {
    "place-market-order", "place-limit-order", "cancel-order", "cancel-all-orders",
    "set-leverage", "set-margin-mode", "place-futures-market-order", "place-futures-limit-order",
    "transfer-funds",
}

# Every field the credential-injecting wrapper supplies itself. Stripped from the schema
# handed to the model so it is never prompted to produce (and therefore never leaks into
# conversation history or a pending_actions row) the real exchange secret.
CCXT_CREDENTIAL_FIELDS = {"exchange", "apiKey", "secret"}


class _CCXTCredentialClient:
    """Wraps the raw stdio client so exchange/apiKey/secret are injected here, on every
    call, from config -- never read from the model's own tool-call arguments. This is
    the ONE place those values are ever assembled into a call; _dispatch_tool_call and
    _resolve_pending_action both just call .call_tool(name, arguments) generically, same
    as every other integration, and never need to know CCXT is different.
    """

    def __init__(self, raw, exchange: str, api_key: str, api_secret: str):
        self._raw = raw
        self._exchange = exchange
        self._api_key = api_key
        self._api_secret = api_secret

    def list_tools(self) -> list[dict]:
        return self._raw.list_tools()

    def call_tool(self, name: str, arguments: dict) -> dict:
        # The real credentials always win, regardless of anything present under these
        # keys in arguments -- they shouldn't be there (the exposed schema has no such
        # properties), but a stray value must never override the configured account.
        merged = {**arguments, "exchange": self._exchange,
                 "apiKey": self._api_key, "secret": self._api_secret}
        return self._raw.call_tool(name, merged)


def _strip_ccxt_credential_fields(schema: dict) -> dict:
    properties = {k: v for k, v in schema.get("properties", {}).items()
                 if k not in CCXT_CREDENTIAL_FIELDS}
    required = [r for r in schema.get("required", []) if r not in CCXT_CREDENTIAL_FIELDS]
    return {**schema, "properties": properties, "required": required}


def build_ccxt_context(cfg) -> CCXTContext | None:
    """Real exchange, real money -- see CCXTContext's docstring for the credential-
    injection design this depends on. Off unless all three of exchange/key/secret are
    configured; there is no partial or read-only-only mode at the config level (a
    deployment that only wants market data has no reason to hold trade-capable keys at
    all, so that case is simply 'don't configure this')."""
    if not cfg.ccxt_exchange or not cfg.ccxt_api_key or not cfg.ccxt_api_secret:
        return None
    exe = _npm_global_bin("mcp-server-ccxt")
    if exe is None:
        logger.warning(
            "CCXT MCP server not found (npm install -g @mcpfun/mcp-server-ccxt) — disabled this session"
        )
        return None
    raw = StdioMCPClient(exe, env={"DEFAULT_EXCHANGE": cfg.ccxt_exchange})
    try:
        discovered = raw.list_tools()
    except Exception as e:
        logger.warning("CCXT MCP server failed to start (%s) — disabled this session", e)
        return None

    tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"], "description": t["description"],
                "parameters": _strip_ccxt_credential_fields(t["input_schema"]),
            },
        }
        for t in discovered
    ]
    sensitive = CCXT_SENSITIVE_TOOL_NAMES & {t["name"] for t in discovered}
    client = _CCXTCredentialClient(raw, cfg.ccxt_exchange, cfg.ccxt_api_key, cfg.ccxt_api_secret)
    logger.info("CCXT: %s, %d tools discovered, %d gated as sensitive (real-money actions)",
               cfg.ccxt_exchange, len(tools), len(sensitive))
    return CCXTContext(mcp_client=client, ccxt_tools=tools, sensitive_tools=sensitive)


# Only the release step (real postage, real cost, unrecallable) is sensitive. send_mail
# only ever preauths -- priced and queued, never sent or charged -- so it and every
# read-only tool (tracking, status, balance) run without a confirmation gate.
LETTERSTREAM_SENSITIVE_TOOLS = {"letterstream_authorize_mail"}


def build_letterstream_context(cfg) -> LetterStreamContext | None:
    if not cfg.letterstream_api_id or not cfg.letterstream_api_key:
        return None
    required_from_fields = (cfg.letterstream_from_name, cfg.letterstream_from_address,
                            cfg.letterstream_from_city, cfg.letterstream_from_state,
                            cfg.letterstream_from_zip)
    if not all(required_from_fields):
        logger.warning(
            "LetterStream is configured but the return address is incomplete "
            "(letterstream_from_name/address/city/state/zip) — disabled this session"
        )
        return None

    from .letterstream_client import LetterStreamClient, LetterStreamTools

    client = LetterStreamClient(cfg.letterstream_api_id, cfg.letterstream_api_key)
    try:
        status = client.account_status()
    except Exception as e:
        logger.warning("LetterStream account check failed (%s) — disabled this session", e)
        return None

    from_addr = {
        "name_1": cfg.letterstream_from_name, "addr_1": cfg.letterstream_from_address,
        "addr_2": cfg.letterstream_from_address_2 or "", "city": cfg.letterstream_from_city,
        "state": cfg.letterstream_from_state, "zip": cfg.letterstream_from_zip,
    }
    tools = LetterStreamTools(client, from_addr)
    logger.info("LetterStream: connected (balance $%s%s)", status.get("balance", "?"),
               ", TEST MODE" if status.get("testmode") == "enabled" else "")
    return LetterStreamContext(mcp_client=tools, sensitive_tools=LETTERSTREAM_SENSITIVE_TOOLS)
