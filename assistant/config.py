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
    """Who the business is and where — everything the background agents need that would
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
    # Recipe API: remote Streamable HTTP MCP server, same shape as Era, bearer-key auth.
    # Read-only/generative (search, filter, lookups, plus generate_recipe) -- nothing here
    # needs sensitive_tools, see RecipeContext's docstring in engine.py.
    recipe_api_key: str | None = None
    recipe_mcp_url: str = "https://recipe-api.com/api/mcp"
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
    # How often the autonomous junk-flagging pass runs (see scheduler.py's
    # mail_junk_scan job) and the score (junk_filter.score_message) a message needs to
    # clear before it's moved into Junk. Both tunable without a code change because
    # real-world false-positive/negative rates only show up once real mail is flowing.
    mail_junk_scan_interval_seconds: int = 900
    mail_junk_score_threshold: float = 4.0
    # How often kitchen_db.sync_kroger_orders runs (see scheduler.py's kroger_sync job).
    # No documented Kroger rate limit exists anywhere in kroger-mcp or its API docs, so
    # this mirrors Era's real-world default rather than inventing a number.
    kroger_sync_interval_seconds: int = 3600
    # Watchdog polling (see scheduler.py's run_task_watchdog/run_review_watchdog and
    # docs/watchdog-system-design.md). Tasks poll fast since it's a cheap mechanical
    # check; Review staleness polls slower since the threshold itself is hours, not
    # seconds -- there's no value in checking every minute for something that only
    # matters after review_watchdog_stale_hours have passed.
    task_watchdog_interval_seconds: int = 60
    review_watchdog_interval_seconds: int = 900
    review_watchdog_stale_hours: float = 2.0
    # 2-5 minutes is GitHub-poll-latency territory per docs/watchdog-system-design.md --
    # far under GitHub's 5,000 req/hr authenticated rate limit at this scale.
    github_watchdog_interval_seconds: int = 180
    # The local-first fast path (assistant/core/local_fast_path.py): tries a local
    # Ollama model before ever reaching the cloud backend for simple, well-scoped
    # requests (Home Assistant commands today). Deliberately separate from
    # ollama_host/ollama_model (used only when llm_backend == "ollama", and shared with
    # the GPU bridge's own host) -- the owner's actual plan is a second, dedicated local
    # LLM box that eventually becomes Jarvis's main backend, and this fast path is where
    # that migration's rough edges (tool-calling reliability, keeping a model warm) get
    # worked out on a narrow, low-stakes slice of traffic first. None/unset means the
    # fast path is disabled and every request behaves exactly as it does today.
    local_llm_host: str | None = None
    local_llm_model: str = "gemma4:12b-it-q4_K_M"
    local_llm_timeout_seconds: float = 20.0
    # How often to ping the local model with a trivial message to keep it resident in
    # VRAM -- Ollama's own keep_alive only resets on each real use, so a quiet period
    # (or another job on the same GPU claiming its memory) can let it fall out, and the
    # very first request after that pays a real ~10s reload cost instead of the
    # sub-second warm response the fast path exists to provide.
    local_llm_keepalive_interval_seconds: int = 600
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
    # Bounds converse()/chat() only -- the owner's own live turn, where a human is
    # actually waiting. research()/engineer()/assign() all override this per-call with
    # their own much larger budget (see staff_assignment_timeout_seconds below), so this
    # doesn't need to protect against a multi-hour background job the way it used to.
    # Briefly dropped to 90s on the theory that an interactive turn is always fast --
    # real usage proved that wrong (a real logged turn with a few chained tool calls,
    # e.g. checking PR/CI status, already ran ~81s; several genuinely timed out at 90s,
    # each one surfacing as "I couldn't reach my reasoning backend" for no real reason).
    # Back to 300s, its long-standing value from before that change.
    claude_timeout_seconds: int = 300
    claude_tools_api_key: str | None = None
    claude_tools_url: str = "http://127.0.0.1:8080/api/tools/call"
    # staff.assign()'s subprocess ceiling for an employee's own assignment (research- or
    # execute-tier) -- deliberately its own knob, separate from claude_timeout_seconds
    # (which bounds the owner's own live conversational turn and should stay short). A
    # real dev-team coding assignment (read the repo, run tools, open a PR) routinely
    # needs far longer than a chat reply, and the previous shared-ish 900s default killed
    # real in-progress jobs via subprocess.run's hard timeout, silently losing the work.
    # Generous rather than unbounded: a genuinely wedged subprocess should still free its
    # slot eventually rather than pin one of staff_cadence's max_instances forever.
    staff_assignment_timeout_seconds: int = 10800
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
    # fetch files) — hence its own host setting.
    comfy_host: str | None = None
    # Voice devices (the Pi terminals). piper_voice_path enables server-side speech;
    # device_api_key authenticates the headless clients, which have no session cookie.
    device_api_key: str | None = None
    # Room presence & identity: YOLO11n (person detection) + insightface (face
    # recognition), targeting the RTX 3060 on the same host as the assistant. The heavy
    # models never load in jarvis-core/jarvis-web's own process -- see
    # core/detector.py and core/face_recognition.py's docstrings -- this config only
    # says which cameras exist and how the pipeline should behave; the actual inference
    # runs in scripts/vision_worker.py, under its own .venv-vision. Each entry in
    # `cameras` is {key, name, url, kind ('mjpeg'|'rtsp'|'local'), location,
    # motion_threshold, recordable}. Omit `cameras` (or leave it empty) and the whole
    # vision side stays inert: no cameras seeded, conversation_mode never returns true.
    cameras: list[dict] = None
    vision_device: str | None = None   # e.g. "cuda:0"; None = auto-detect
    camera_poll_seconds: float = 1.5
    face_match_threshold: float = 0.42
    enroll_after_sightings: int = 3
    # Which voice device's room a camera watches, for open-mic gating -- day one, Touch1
    # and laptop1 are both their own camera and their own voice terminal, so the map
    # starts as the identity map. Override in config.json if a device and its camera
    # ever diverge (e.g. a device with no camera of its own, watched by a neighbouring
    # one).
    device_camera_map: dict = field(default_factory=lambda: {"touch1": "touch1", "laptop1": "laptop1"})
    # Room Presence & Identity gating for an unattended voice terminal's /turn (see
    # core/presence.py) -- how recent a camera confirmation counts, and which
    # known_people.access_level values unlock personal/financial context. Defaults match
    # presence.py's own DEFAULT_AUTHORIZED_ACCESS_LEVELS/confirmed_identity default so a
    # deployment that never sets these gets the same fail-closed behaviour the module
    # documents. Master switch: the original design promised this stays a genuine no-op
    # on every /turn until the owner opts in ("presence_identity_enabled", default
    # false) -- off means every voice terminal behaves exactly as it did before this
    # feature existed (always answers as the owner, full context, no camera required).
    presence_identity_enabled: bool = False
    presence_confirm_window_seconds: int = 45
    presence_authorized_access_levels: list[str] = field(default_factory=lambda: ["owner"])
    # Cross-terminal wake-word arbitration (core/wake_arbitration.py) -- how long a
    # terminal waits to see a louder rival claim before proceeding, and the margin a
    # rival needs to clearly beat it by. Defaults match wake_arbitration.py's own
    # DEFAULT_WINDOW_MS/DEFAULT_MARGIN.
    wake_arbitration_window_ms: int = 400
    wake_arbitration_margin: float = 0.05
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
    # Dev-team agents: git branch/push/PR tools (Phase 1) and later the ops-plan/SSH
    # workflow both need a repo to target and a token to act on the owner's behalf.
    # Fine-grained PAT scoped to just this one repo, Contents + Pull requests read/write.
    github_repo: str | None = None
    github_pat: str | None = None
    # Where the local working clone (and one git-worktree checkout per branch) lives.
    # Kept outside the app's own repo tree so an employee's workspace is never itself
    # a nested repo Jarvis's own git history would need to account for.
    git_workspace_path: str = "../jarvis-git-workspace"
    git_author_name: str = "Jarvis"
    git_author_email: str = "jarvis@localhost"
    scan_ssh_password: str | None = None
    scan_ssh_users: list[str] = field(default_factory=lambda: ["pi", "jack"])
    scan_subnet: str = "192.168.0"
    # Named registry of hosts the ops-plan workflow (assistant/core/ssh_ops.py) may
    # target: {name: {"host", "user", "key_path"?, "password"?}}. The employee/model only
    # ever refers to a host by its short name here -- the real address and credential are
    # injected server-side, same principle as every other credential-injecting wrapper in
    # this codebase (CCXT, git_ops), never a tool argument the model could leak or forge.
    ssh_hosts: dict = field(default_factory=dict)
    piper_voice_path: str | None = None
    generated_media_path: str = "generated"
    # Master switch for unattended agent runs. Off by default and currently off in the
    # real config: the owner's call is that nothing should fire on a timer until he and
    # Jarvis have worked out sensible cadence and working hours together. Every agent
    # still runs on demand from chat via run_business_agent — this only governs the
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
        recipe_api_key=data.get("recipe_api_key"),
        recipe_mcp_url=data.get("recipe_mcp_url", "https://recipe-api.com/api/mcp"),
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
        # Matches the phone/Era gating policy: only tools with a real, hard-to-fully-undo
        # consequence require confirmation. send_email leaves the account for good;
        # archive_email/delete_email each remove a message from wherever the user currently
        # has it (delete lands it in Trash, not a true wipe, but it's still gone from view
        # without warning if unconfirmed) — read tools (list/search/read/list_mail_folders)
        # and mark_email_read (just a flag, trivially reversible) run directly.
        mail_sensitive_tools=data.get("mail_sensitive_tools", ["send_email", "archive_email", "delete_email"]),
        # Junk-flagging (move to Junk folder) is deliberately NOT in mail_sensitive_tools:
        # it's reversible (nothing is deleted) and the whole point of "autonomous" triage
        # is that it doesn't wait on a chat confirmation for every scan.
        mail_junk_scan_interval_seconds=data.get("mail_junk_scan_interval_seconds", 900),
        mail_junk_score_threshold=data.get("mail_junk_score_threshold", 4.0),
        kroger_sync_interval_seconds=data.get("kroger_sync_interval_seconds", 3600),
        task_watchdog_interval_seconds=data.get("task_watchdog_interval_seconds", 60),
        review_watchdog_interval_seconds=data.get("review_watchdog_interval_seconds", 900),
        review_watchdog_stale_hours=data.get("review_watchdog_stale_hours", 2.0),
        github_watchdog_interval_seconds=data.get("github_watchdog_interval_seconds", 180),
        local_llm_host=data.get("local_llm_host"),
        local_llm_model=data.get("local_llm_model", "gemma4:12b-it-q4_K_M"),
        local_llm_timeout_seconds=data.get("local_llm_timeout_seconds", 20.0),
        local_llm_keepalive_interval_seconds=data.get("local_llm_keepalive_interval_seconds", 600),
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
        staff_assignment_timeout_seconds=data.get("staff_assignment_timeout_seconds", 10800),
        # Authenticates the MCP bridge subprocess to /api/tools/call. Equivalent to full
        # owner access — anything holding it can invoke every tool Jarvis has.
        claude_tools_api_key=data.get("claude_tools_api_key"),
        claude_tools_url=data.get("claude_tools_url", "http://127.0.0.1:8080/api/tools/call"),
        # Omit the "business" block entirely and the whole second-in-command side stays
        # off — no tools offered, no agents scheduled, no digest.
        business=BusinessProfile(**data["business"]) if data.get("business") else None,
        gpu_bridge_enabled=data.get("gpu_bridge_enabled", True),
        gpu_bridge_host=data.get("gpu_bridge_host") or data.get("ollama_host"),
        gpu_task_models=data.get("gpu_task_models"),
        gpu_max_concurrent=data.get("gpu_max_concurrent", 2),
        comfy_host=data.get("comfy_host"),
        device_api_key=data.get("device_api_key"),
        cameras=data.get("cameras"),
        vision_device=data.get("vision_device"),
        camera_poll_seconds=data.get("camera_poll_seconds", 1.5),
        face_match_threshold=data.get("face_match_threshold", 0.42),
        enroll_after_sightings=data.get("enroll_after_sightings", 3),
        device_camera_map=data.get("device_camera_map", {"touch1": "touch1", "laptop1": "laptop1"}),
        presence_identity_enabled=data.get("presence_identity_enabled", False),
        presence_confirm_window_seconds=data.get("presence_confirm_window_seconds", 45),
        presence_authorized_access_levels=data.get("presence_authorized_access_levels", ["owner"]),
        wake_arbitration_window_ms=data.get("wake_arbitration_window_ms", 400),
        wake_arbitration_margin=data.get("wake_arbitration_margin", 0.05),
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
        github_repo=data.get("github_repo"),
        github_pat=data.get("github_pat"),
        git_workspace_path=data.get("git_workspace_path", "../jarvis-git-workspace"),
        git_author_name=data.get("git_author_name", "Jarvis"),
        git_author_email=data.get("git_author_email", "jarvis@localhost"),
        scan_ssh_password=data.get("scan_ssh_password"),
        scan_ssh_users=data.get("scan_ssh_users", ["pi", "jack"]),
        scan_subnet=data.get("scan_subnet", "192.168.0"),
        ssh_hosts=data.get("ssh_hosts", {}),
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
