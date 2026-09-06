"""LLM backend that drives the Claude Code CLI instead of Ollama.

Chosen so Jarvis runs on the user's existing claude.ai subscription rather than
per-token API billing, and to get off the local model's latency until the new hardware
lands. `claude auth status` reports authMethod=claude.ai, so no API key is involved.

The important structural difference from LLMClient: Claude Code runs its OWN agent loop.
It does not take an OpenAI-style `tools` list and hand back `tool_calls` for a caller to
execute — it calls tools itself, over MCP. So this class exposes `converse()` (give it
the conversation, get finished text back) and sets `agentic = True`, which
engine.handle_message uses to skip its hop loop. Jarvis's tools reach that subprocess
through assistant/mcp_bridge.py, which forwards them to the running web process so
confirmation gating stays in engine._dispatch_tool_call for both backends.

`chat()` is still implemented, with LLMClient's signature, because engine's confirmation
classifier calls llm.chat() directly for a plain yes/no judgement. Tool arguments are
ignored there — that path never uses them.

Sandboxing is not optional here. This subprocess is driven by conversation content, and
Claude Code ships with Bash, file editing, subagents, and cross-session messaging. Left
at defaults it will use them: while testing this integration, a sandboxed instance
denied Bash first offered to spawn a subagent with shell access, and when that was
denied too, messaged a peer Claude session on the machine asking it to run the command
instead. Both holes are closed below by allowlisting only Jarvis's own MCP tools and
explicitly denying every built-in capability, rather than relying on --restricted alone.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

# Everything Claude Code can do that isn't a Jarvis tool. --restricted already removes
# Bash/Edit/Write, but it leaves the subagent and session-messaging tools, which are
# escape hatches to exactly those capabilities (see module docstring). Names that don't
# exist in a given CLI version are simply ignored, so this list is safe to over-specify
# and should stay over-specified as new built-ins appear.
# WebSearch is deliberately NOT denied — it's how Jarvis answers news and general
# current-events questions, with no integration to build and no API key. WebFetch stays
# denied: search returns model-summarised results, whereas fetch pulls a full arbitrary
# page into the context of an assistant that can send email and control the house, which
# is a materially larger prompt-injection surface for little extra benefit here.
DENIED_TOOLS = [
    "Bash", "BashOutput", "KillShell", "Edit", "Write", "NotebookEdit", "Read", "Glob", "Grep",
    "Task", "Agent", "WebFetch", "SlashCommand", "Skill", "SendMessage", "ListAgents",
    "SendUserFile", "Artifact", "ArtifactComments", "ArtifactData", "ArtifactCheck", "Workflow",
    "CronCreate", "CronDelete", "CronList", "RemoteTrigger", "Monitor", "TaskOutput", "TaskStop",
    "PushNotification", "DesignSync", "EnterPlanMode", "ExitPlanMode", "EnterWorktree",
    "ExitWorktree", "ScheduleWakeup", "ReportFindings", "TodoWrite", "ToolSearch",
]

DEFAULT_CLI_PATH = os.path.expanduser(r"~\.local\bin\claude.exe")


class ClaudeCLIClient:
    agentic = True
    # Tells engine.build_system_prompt this backend can actually search the web, so the
    # capability is only ever advertised where it genuinely exists.
    web_search = True

    def __init__(
        self, cli_path: str | None = None, model: str = "sonnet", tools_url: str | None = None,
        tools_token: str | None = None, user_id: int | None = None, timeout: int = 300,
    ):
        self.cli_path = cli_path or shutil.which("claude") or DEFAULT_CLI_PATH
        self.model = model
        self.tools_url = tools_url
        self.tools_token = tools_token
        self.user_id = user_id
        self.timeout = timeout

    # ---- internals -------------------------------------------------------

    def _base_command(self, system_prompt: str, prompt: str) -> list[str]:
        return [
            self.cli_path, "-p", prompt,
            "--model", self.model,
            # Replaces Claude Code's built-in coding-agent system prompt outright rather
            # than appending to it — Jarvis is not a coding agent and shouldn't inherit
            # those instructions.
            "--system-prompt", system_prompt,
            "--restricted",
            # Ignore any MCP servers configured globally for the user's own Claude Code
            # use; this subprocess gets Jarvis's bridge and nothing else.
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--output-format", "json",
        ]

    def _run(self, command: list[str], env: dict | None = None, cwd: str | None = None) -> str:
        """Runs the CLI and returns the final assistant text.

        Failures raise, so handle_message can report them honestly rather than passing a
        blank or half-formed answer off as an answer.
        """
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout, env=env, cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"the request took longer than {self.timeout}s")

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RuntimeError(detail[0] if detail else f"claude exited {proc.returncode}")

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError("could not parse the CLI's response")

        if payload.get("is_error"):
            raise RuntimeError(payload.get("result") or "the CLI reported an error")

        usage = payload.get("usage") or {}
        logger.info(
            "claude turn: %s turns, %sms, cache_read=%s cache_write=%s out=%s",
            payload.get("num_turns"), payload.get("duration_ms"),
            usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"),
            usage.get("output_tokens"),
        )
        return payload.get("result") or ""

    @staticmethod
    def _render(history: list[dict], now: str, tz_name: str) -> str:
        """Renders Jarvis's DB history into the single prompt string print mode takes.

        Jarvis's own database stays the source of truth for conversation history across
        every surface (web, Telegram, Home Assistant), so the CLI's own session store is
        deliberately not used — no --resume, no session ids to keep in sync.

        The current time goes here rather than in the system prompt on purpose: Anthropic
        prompt-caches on an exact prefix, and a timestamp in the system prompt would
        invalidate that cache every single turn, re-billing Claude Code's ~25k-token
        preamble instead of reading it back cheaply.
        """
        lines = []
        # The last entry is the message being answered; earlier ones are context.
        prior, current = history[:-1], (history[-1] if history else {"content": ""})
        if prior:
            lines.append("<conversation_history>")
            for message in prior:
                speaker = "Jarvis" if message.get("role") == "assistant" else "User"
                lines.append(f"{speaker}: {message.get('content', '')}")
            lines.append("</conversation_history>\n")
        lines.append(f"The current local time is {now} ({tz_name}).\n")
        lines.append("Reply to this message from the user:")
        lines.append(current.get("content", ""))
        return "\n".join(lines)

    def _setup_tool_bridge(self, workdir: str, tools: list[dict], env: dict, command: list[str]) -> None:
        """Writes the tool schema + MCP config the bridge (mcp_bridge.py) reads, and
        points this turn's env at it. Shared by converse() (the owner's own full
        catalog) and engineer() (an execute-tier employee's narrow, purpose-built set)
        -- whichever list is written here is the entire universe of MCP tools the
        subprocess can see, regardless of what --allowed-tools says, since the bridge
        only ever serves what's in this file."""
        schema_path = os.path.join(workdir, "tools.json")
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(tools, f)
        bridge = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_bridge.py")
        mcp_config = {"mcpServers": {"jarvis": {
            "command": os.environ.get("JARVIS_PYTHON") or _python_executable(),
            "args": [bridge],
        }}}
        config_path = os.path.join(workdir, "mcp.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(mcp_config, f)
        command += ["--mcp-config", config_path]
        env.update({
            "JARVIS_TOOLS_SCHEMA": schema_path,
            "JARVIS_TOOLS_URL": self.tools_url or "",
            "JARVIS_TOOLS_TOKEN": self.tools_token or "",
            "JARVIS_USER_ID": str(self.user_id if self.user_id is not None else ""),
        })

    # ---- public API ------------------------------------------------------

    def converse(
        self, system_prompt: str, history: list[dict], now: str, tz_name: str,
        tools: list[dict] | None = None, image_bytes: bytes | None = None,
    ) -> str:
        """One full agentic turn: Claude reasons, calls Jarvis's tools as needed, and
        returns finished reply text."""
        prompt = self._render(history, now, tz_name)
        env = dict(os.environ)
        allowed = ["mcp__jarvis", "WebSearch"]
        denied = list(DENIED_TOOLS)

        # A fresh directory per turn is the sandbox's floor: --restricted confines file
        # tools to the working directory, so if a camera snapshot makes Read necessary,
        # the only thing in reach is that snapshot.
        with tempfile.TemporaryDirectory(prefix="jarvis-claude-") as workdir:
            command = self._base_command(system_prompt, prompt)

            if tools:
                self._setup_tool_bridge(workdir, tools, env, command)

            if image_bytes is not None:
                image_path = os.path.join(workdir, "snapshot.jpg")
                with open(image_path, "wb") as f:
                    f.write(image_bytes)
                denied.remove("Read")
                allowed.append("Read")
                command[2] += (
                    f"\n\nThe user attached a photo from their camera with this message. "
                    f"Read the image file 'snapshot.jpg' in the current directory to see it, "
                    f"and take it into account when replying."
                )

            command += ["--allowed-tools", ",".join(allowed), "--disallowed-tools", ",".join(denied)]
            return self._run(command, env=env, cwd=workdir)

    def research(self, instructions: str, system_prompt: str | None = None, timeout: int | None = None) -> str:
        """One-shot research task with web search, for the background agents.

        Distinct from converse(): no conversation history, no Jarvis tools, and a longer
        timeout, because a market or trend scan runs several searches and is nobody's
        latency-sensitive path. Jarvis's own tools are deliberately absent — an agent
        running unattended on a timer should gather and report, not quietly send email
        or change the house. Anything actionable comes back as a row the owner reviews.
        """
        command = self._base_command(
            system_prompt or "You are a research assistant. Be accurate and concise.", instructions,
        )
        command += [
            "--allowed-tools", "WebSearch",
            "--disallowed-tools", ",".join(t for t in DENIED_TOOLS if t != "WebSearch"),
        ]
        previous_timeout = self.timeout
        try:
            if timeout:
                self.timeout = timeout
            return self._run(command)
        finally:
            self.timeout = previous_timeout

    def engineer(
        self, instructions: str, system_prompt: str, tools: list[dict], timeout: int | None = None,
    ) -> str:
        """One-shot dev-team task with real tool access, for 'execute'-tier employees
        (staff.py) -- the one employee capability that can act rather than only report.

        Distinct from research(): this can call tools, but strictly limited to whatever
        purpose-built list the caller passes in (git branch/push/PR today; SSH/ops-plan
        tools later) -- never the owner's full catalog, and never Bash/Write/Edit/Task/
        Agent/SendMessage, which stay in DENIED_TOOLS exactly as for every other
        invocation. Distinct from converse(): no conversation history, since this is a
        one-shot assignment rather than a back-and-forth chat.

        Tool calls authenticate to the bridge as whoever this client was built for
        (self.user_id/tools_token, set once at startup) -- the same owner identity
        converse() uses -- so a merge_pr call from here lands as a pending_action the
        owner sees in his own chat, exactly as if he'd asked for it himself.
        """
        env = dict(os.environ)
        denied = list(DENIED_TOOLS)

        with tempfile.TemporaryDirectory(prefix="jarvis-engineer-") as workdir:
            command = self._base_command(system_prompt, instructions)
            self._setup_tool_bridge(workdir, tools, env, command)
            command += ["--allowed-tools", "mcp__jarvis,WebSearch", "--disallowed-tools", ",".join(denied)]

            previous_timeout = self.timeout
            try:
                if timeout:
                    self.timeout = timeout
                return self._run(command, env=env, cwd=workdir)
            finally:
                self.timeout = previous_timeout

    def chat(self, messages: list[dict], tools: list[dict] | None = None, think: bool = False) -> dict:
        """LLMClient-compatible single-shot call, for engine's confirmation classifier.

        No tools are exposed regardless of the `tools` argument: this path only ever asks
        for a plain-text judgement, and an unsandboxed tool call here would sidestep the
        very confirmation gate it exists to serve.
        """
        system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        body = "\n".join(
            f"{'Jarvis' if m.get('role') == 'assistant' else 'User'}: {m.get('content', '')}"
            for m in messages if m.get("role") != "system"
        )
        command = self._base_command(system or "You are Jarvis.", body)
        command += ["--disallowed-tools", ",".join(DENIED_TOOLS)]
        return {"role": "assistant", "content": self._run(command)}


def _python_executable() -> str:
    import sys

    return sys.executable
