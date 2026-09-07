"""Loads runtime configuration (bot token, user allowlist, Ollama endpoint) from a JSON
file kept out of source control, so the Telegram token and the two chat_ids never end up
committed.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UserConfig:
    telegram_chat_id: str
    display_name: str
    role: str  # "owner" | "partner"
    web_password: str | None = None


@dataclass
class BusinessProfile:
    """Who the business is and where â€” everything the background agents need that would
    otherwise be hardcoded. The predecessor project (Blue Ridge Custom Co) baked Asheville
    into its market-finder, which made the whole module worthless the moment the business
    moved cities. None of that belongs in code."""

    name: str
    location: str
    radius_miles: int = 60
    product_lines: list[str] = None
    notes: str | None = None
    min_fit_score: int = 40

    def __post_init__(self):
        if self.product_lines is None:
            self.product_lines = []


@dataclass
class Config:
    telegram_bot_token: str
    ollama_host: str
    ollama_model: str
    db_path: str
    users: list[UserConfig]
    poll_interval_seconds: int = 30
    timezone: str = "America/New_York"
    era_api_key: str | None = None
    era_mcp_url: str = "https://context.era.app/mcp"
    era_sensitive_tools: list[str] = None
    apple_id: str | None = None
    apple_app_password: str | None = None
    caldav_url: str = "https://caldav.icloud.com"
    caldav_personal_calendar: str | None = None
    caldav_shared_calendar: str | None = None
    caldav_sync_interval_seconds: int = 300
    era_cache_interval_seconds: int = 3600
    web_session_secret: str | None = None
    web_port: int = 8080
    phone_mcp_url: str | None = None
    phone_sensitive_tools: list[str] = None
    stt_model_size: str = "small.en"
    mail_sensitive_tools: list[str] = None
    # Autonomous email triage (junk-flagging/draft-reply/auto-filing -- mail_tools.py's
    # MailTriageClient). A separate sensitive_tools set from mail_sensitive_tools above
    # because it's logically a distinct set of tools layered on the same mail account;
    # kept to the mutating ones (moving/flagging a real message, saving a real draft),
    # same reasoning as mail_sensitive_tools only gating send_email.
    mail_triage_sensitive_tools: list[str] = None
    # Junk-likelihood score (0-1) at or above which a message is treated as spam_junk.
    # See mail_triage.score_junk's docstring for what feeds the score.
    mail_junk_score_threshold: float = 0.5
    # Overrides mail_triage.DEFAULT_CATEGORY_FOLDERS per-category, e.g. to point
    # "order_inquiry" at a folder actually named "Print Orders" in the real account.
    # Categories omitted here keep the built-in default.
    mail_category_folders: dict | None = None
    obsidian_vault_path: str | None = None
    ha_base_url: str | None = None
    ha_token: str | None = None
    ha_sensitive_domains: list[str] = None
    ha_conversation_api_key: str | None = None
    # Which notify service to push to by default (e.g. notify.mobile_app_upstream).
    ha_notify_target: str | None = None
    # How often to check where he is. Arriving somewhere isn't a sub-minute event, and
    # each poll is one HA request.
    location_poll_seconds: int = 120
    # How often to actively wake the phone for a fresh fix. Each one is a push, so this
    # is deliberately much slower than the passive poll, and only happens when a
    # location reminder or routine is actually armed.
    location_force_seconds: int = 600
    llm_backend: str = "ollama"  # "ollama" | "claude_cli"
    claude_cli_path: str | None = None
    claude_model: str = "sonnet"
    claude_timeout_seconds: int = 300
    claude_tools_api_key: str | None = None
    claude_tools_url: str = "http://127.0.0.1:8080/api/tools/call"
    business: BusinessProfile | None = None
    # simrig as a shared inference resource for agent work that doesn't need Claude.
    # Defaults to the same host Ollama already runs on. gpu_task_models overrides the
    # task -> model routing in gpu_bridge.DEFAULT_TASK_MODELS, which is where model
    # choices belong: that list ages in weeks.
    gpu_bridge_enabled: bool = True
    gpu_bridge_host: str | None = None
    gpu_task_models: dict | None = None
    gpu_max_concurrent: int = 2
    # ComfyUI on simrig, for image and video generation. A separate server from Ollama
    # on the same box, speaking a completely different protocol (queue a graph, poll,
    # fetch files) â€” hence its own host setting.
    comfy_host: str | None = None
    # Voice devices (the Pi terminals). piper_voice_path enables server-side speech;
    # device_api_key authenticates the headless clients, which have no session cookie.
    device_api_key: str | None = None
    # Shared bearer token Home Assistant's actionable-notification automation sends.
    notification_token: str | None = None
    # SSH login used to sweep the house Pis and Ubuntu boxes for media. One password
    # covers every box; the username differs (pi vs jack) and is discovered per host.
    # LiveCoinWatch feeds the crypto agents. One poller fills a local cache; the agents
    # read that rather than the API, so their spend is bounded and they get history a
    # single response cannot give.
    livecoinwatch_api_key: str | None = None
    market_poll_seconds: int = 60
    market_track_limit: int = 250
    # Third-party MCP servers (assistant/core/mcp_stdio_client.py), each a spawned child
    # process reached over stdio rather than the Streamable HTTP the Era/phone/HA MCP
    # servers use. None of these fields being set is the normal, safe default -- each
    # integration is off until real credentials are dropped in.
    airbnb_mcp_enabled: bool = False
    ticketmaster_api_key: str | None = None
    # Kroger needs a one-time OAuth authorization (start_authentication /
    # complete_authentication) beyond just these -- see KROGER_SYSTEM_NOTE in engine.py.
    # redirect_uri only needs to be a syntactically valid localhost URL Kroger can bounce
    # the browser to; nothing has to be listening on it for the code-in-the-URL flow to work.
    kroger_client_id: str | None = None
    kroger_client_secret: str | None = None
    kroger_redirect_uri: str = "http://localhost:8000/callback"
    # CCXT: real money on a real exchange. See CCXTContext's docstring (engine.py) and
    # setup.py's _CCXTCredentialClient for why the secret lives only here and in the
    # wrapper that injects it, never in a tool schema the model sees.
    ccxt_exchange: str | None = None
    ccxt_api_key: str | None = None
    ccxt_api_secret: str | None = None
    # LetterStream: physical mail (see letterstream_client.py). The return address is
    # fixed in config rather than supplied per letter by the model -- it never changes,
    # and asking the model to restate it each time is one more place a typo could put
    # the wrong return address on a real piece of mail.
    letterstream_api_id: str | None = None
    letterstream_api_key: str | None = None
    letterstream_from_name: str | None = None
    letterstream_from_address: str | None = None
    letterstream_from_address_2: str | None = None
    letterstream_from_city: str | None = None
    letterstream_from_state: str | None = None
    letterstream_from_zip: str | None = None
    scan_ssh_password: str | None = None
    scan_ssh_users: list[str] = field(default_factory=lambda: ["pi", "jack"])
    scan_subnet: str = "192.168.0"
    piper_voice_path: str | None = None
    generated_media_path: str = "generated"
    # Master switch for unattended agent runs. Off by default and currently off in the
    # real config: the owner's call is that nothing should fire on a timer until he and
    # Jarvis have worked out sensible cadence and working hours together. Every agent
    # still runs on demand from chat via run_business_agent â€” this only governs the
    # scheduler. The intervals below are what takes effect when it's switched back on.
    business_agents_enabled: bool = False
    # Deliberately unhurried when enabled. These scans each make many web searches billed
    # against a Claude subscription, and a maker business does not change hour to hour.
    market_scan_interval_hours: int = 72
    trend_scan_interval_hours: int = 24
    research_queue_interval_minutes: int = 120
    pipeline_interval_hours: int = 12
    business_digest_hour: int = 8
    # Personal errands ("find me a doctor") are core owner functionality, not gated behind
    # business_agents_enabled — that switch is about the print business's unattended agents,
    # and nesting this under it would make personal research silently never run by default.
    personal_research_interval_minutes: int = 30


def load_config(path: str = "config.json") -> Config:
    data = json.loads(Path(path).read_text())
    users = [UserConfig(**u) for u in data["users"]]
    return Config(
        telegram_bot_token=data["telegram_bot_token"],
        ollama_host=data["ollama_host"],
        ollama_model=data.get("ollama_model", "qwen3:8b"),
        db_path=data.get("db_path", "jarvis.db"),
        users=users,
        poll_interval_seconds=data.get("poll_interval_seconds", 30),
        timezone=data.get("timezone", "America/New_York"),
        era_api_key=data.get("era_api_key"),
        era_mcp_url=data.get("era_mcp_url", "https://context.era.app/mcp"),
        era_sensitive_tools=data.get("era_sensitive_tools", []),
        apple_id=data.get("apple_id"),
        apple_app_password=data.get("apple_app_password"),
        caldav_url=data.get("caldav_url", "https://caldav.icloud.com"),
        caldav_personal_calendar=data.get("caldav_personal_calendar"),
        caldav_shared_calendar=data.get("caldav_shared_calendar"),
        caldav_sync_interval_seconds=data.get("caldav_sync_interval_seconds", 300),
        era_cache_interval_seconds=data.get("era_cache_interval_seconds", 3600),
        web_session_secret=data.get("web_session_secret"),
        web_port=data.get("web_port", 8080),
        phone_mcp_url=data.get("phone_mcp_url"),
        # Default matches the confirmation-gating policy chosen for the phone MCP integration:
        # actions with real-world consequences (sending a text, placing a call, running a shell
        # command) require explicit confirmation; everything else (camera, mic, location, contacts,
        # SMS read, call log, device controls) executes immediately.
        phone_sensitive_tools=data.get("phone_sensitive_tools", ["send_sms", "make_call", "shell"]),
        stt_model_size=data.get("stt_model_size", "small.en"),
        # Matches the phone/Era gating policy: only send_email has real-world consequences
        # (an email actually leaving the account) â€” read tools (list/search/read) run directly.
        mail_sensitive_tools=data.get("mail_sensitive_tools", ["send_email"]),
        # The triage feature's mutating tools (moving/flagging real mail, saving a real
        # draft) get the same conservative treatment; triage_inbox/draft_reply_email/
        # list_mail_triage_log are read-only or advisory and are deliberately not here.
        mail_triage_sensitive_tools=data.get(
            "mail_triage_sensitive_tools", ["flag_junk_email", "file_email", "save_draft_email"],
        ),
        mail_junk_score_threshold=data.get("mail_junk_score_threshold", 0.5),
        mail_category_folders=data.get("mail_category_folders"),
        obsidian_vault_path=data.get("obsidian_vault_path"),
        ha_base_url=data.get("ha_base_url"),
        ha_token=data.get("ha_token"),
        # Physically-consequential domains stage for confirmation, same policy as
        # send_sms/make_call/send_email; everything else (lights, switches, climate...) runs directly.
        ha_sensitive_domains=data.get("ha_sensitive_domains", ["lock", "cover", "alarm_control_panel"]),
        ha_conversation_api_key=data.get("ha_conversation_api_key"),
        ha_notify_target=data.get("ha_notify_target"),
        location_poll_seconds=data.get("location_poll_seconds", 120),
        location_force_seconds=data.get("location_force_seconds", 600),
        # "claude_cli" runs Jarvis on the Claude Code CLI (and so the user's claude.ai
        # subscription) instead of the local Ollama model. Ollama stays the default and
        # stays wired, so switching back is a one-line config change once the new
        # inference hardware is in.
        llm_backend=data.get("llm_backend", "ollama"),
        claude_cli_path=data.get("claude_cli_path"),
        claude_model=data.get("claude_model", "sonnet"),
        claude_timeout_seconds=data.get("claude_timeout_seconds", 300),
        # Authenticates the MCP bridge subprocess to /api/tools/call. Equivalent to full
        # owner access â€” anything holding it can invoke every tool Jarvis has.
        claude_tools_api_key=data.get("claude_tools_api_key"),
        claude_tools_url=data.get("claude_tools_url", "http://127.0.0.1:8080/api/tools/call"),
        # Omit the "business" block entirely and the whole second-in-command side stays
        # off â€” no tools offered, no agents scheduled, no digest.
        business=BusinessProfile(**data["business"]) if data.get("business") else None,
        gpu_bridge_enabled=data.get("gpu_bridge_enabled", True),
        gpu_bridge_host=data.get("gpu_bridge_host") or data.get("ollama_host"),
        gpu_task_models=data.get("gpu_task_models"),
        gpu_max_concurrent=data.get("gpu_max_concurrent", 2),
        comfy_host=data.get("comfy_host"),
        device_api_key=data.get("device_api_key"),
        notification_token=data.get("notification_token"),
        livecoinwatch_api_key=data.get("livecoinwatch_api_key"),
        market_poll_seconds=data.get("market_poll_seconds", 60),
        market_track_limit=data.get("market_track_limit", 250),
        airbnb_mcp_enabled=data.get("airbnb_mcp_enabled", False),
        ticketmaster_api_key=data.get("ticketmaster_api_key"),
        kroger_client_id=data.get("kroger_client_id"),
        kroger_client_secret=data.get("kroger_client_secret"),
        kroger_redirect_uri=data.get("kroger_redirect_uri", "http://localhost:8000/callback"),
        ccxt_exchange=data.get("ccxt_exchange"),
        ccxt_api_key=data.get("ccxt_api_key"),
        ccxt_api_secret=data.get("ccxt_api_secret"),
        letterstream_api_id=data.get("letterstream_api_id"),
        letterstream_api_key=data.get("letterstream_api_key"),
        letterstream_from_name=data.get("letterstream_from_name"),
        letterstream_from_address=data.get("letterstream_from_address"),
        letterstream_from_address_2=data.get("letterstream_from_address_2"),
        letterstream_from_city=data.get("letterstream_from_city"),
        letterstream_from_state=data.get("letterstream_from_state"),
        letterstream_from_zip=data.get("letterstream_from_zip"),
        scan_ssh_password=data.get("scan_ssh_password"),
        scan_ssh_users=data.get("scan_ssh_users", ["pi", "jack"]),
        scan_subnet=data.get("scan_subnet", "192.168.0"),
        piper_voice_path=data.get("piper_voice_path"),
        generated_media_path=data.get("generated_media_path", "generated"),
        business_agents_enabled=data.get("business_agents_enabled", False),
        market_scan_interval_hours=data.get("market_scan_interval_hours", 72),
        trend_scan_interval_hours=data.get("trend_scan_interval_hours", 24),
        research_queue_interval_minutes=data.get("research_queue_interval_minutes", 120),
        pipeline_interval_hours=data.get("pipeline_interval_hours", 12),
        business_digest_hour=data.get("business_digest_hour", 8),
        personal_research_interval_minutes=data.get("personal_research_interval_minutes", 30),
    )
