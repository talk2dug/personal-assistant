"""Covers the Claude CLI backend's command construction and sandboxing.

The sandbox assertions here are not ceremony. While this integration was being built, a
sandboxed Claude instance denied Bash offered to spawn a subagent with shell access, and
when Task was denied too, it messaged a peer Claude session on the machine asking that
session to run the command on its behalf. Both paths are closed by the deny list, so
these tests pin that list against silent regressions.
"""
import json
from unittest.mock import patch

import pytest

from assistant.core.claude_cli import DENIED_TOOLS, ClaudeCLIClient


class FakeCompleted:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def ok(result="Very good, sir.", **extra):
    return FakeCompleted(json.dumps({"result": result, "is_error": False, **extra}))


@pytest.fixture
def client():
    return ClaudeCLIClient(
        cli_path="claude.exe", model="sonnet", tools_url="http://127.0.0.1:8080/api/tools/call",
        tools_token="tok", user_id=7,
    )


def test_agentic_flag_is_set(client):
    """engine.handle_message branches on this to skip its own tool loop."""
    assert client.agentic is True


def test_render_marks_last_message_as_the_one_to_answer(client):
    history = [
        {"role": "user", "content": "what's the weather"},
        {"role": "assistant", "content": "Clear, sir."},
        {"role": "user", "content": "and tomorrow?"},
    ]
    prompt = client._render(history, now="2026-09-03T10:00:00", tz_name="America/New_York")
    assert "<conversation_history>" in prompt
    assert "User: what's the weather" in prompt
    assert "Jarvis: Clear, sir." in prompt
    # The current turn is the instruction, not part of the quoted history block.
    assert prompt.index("</conversation_history>") < prompt.index("and tomorrow?")
    assert prompt.rstrip().endswith("and tomorrow?")


def test_render_omits_history_block_for_a_first_message(client):
    prompt = client._render([{"role": "user", "content": "hello"}], now="t", tz_name="America/New_York")
    assert "<conversation_history>" not in prompt
    assert prompt.rstrip().endswith("hello")


def test_current_time_goes_in_the_prompt_not_the_system_prompt(client):
    """A timestamp in the system prompt would bust Anthropic's prompt cache every turn,
    re-billing Claude Code's ~25k-token preamble instead of reading it back cheaply."""
    with patch("subprocess.run", return_value=ok()) as run:
        client.converse(
            system_prompt="You are Jarvis.", history=[{"role": "user", "content": "hi"}],
            now="2026-09-03T10:00:00", tz_name="America/New_York",
        )
    command = run.call_args.args[0]
    system_prompt = command[command.index("--system-prompt") + 1]
    assert "2026-09-03T10:00:00" not in system_prompt
    assert "2026-09-03T10:00:00" in command[2]


def test_sandbox_denies_every_known_escape_path(client):
    with patch("subprocess.run", return_value=ok()) as run:
        client.converse(
            system_prompt="You are Jarvis.", history=[{"role": "user", "content": "hi"}],
            now="t", tz_name="America/New_York",
        )
    command = run.call_args.args[0]
    denied = command[command.index("--disallowed-tools") + 1].split(",")
    allowed = command[command.index("--allowed-tools") + 1].split(",")

    # Direct execution, and the two indirect routes to it seen in real testing.
    for escape in ("Bash", "Task", "Agent", "SendMessage", "ListAgents", "Edit", "Write"):
        assert escape in denied, f"{escape} must be denied — it is a path to arbitrary execution"
    # WebSearch is allowed on purpose (news/current events). WebFetch is not: search
    # returns summarised results, whereas fetch pulls a full arbitrary page into the
    # context of an assistant that can send email and control the house.
    assert allowed == ["mcp__jarvis", "WebSearch"]
    assert "WebFetch" in denied
    assert "--restricted" in command
    # Global MCP servers configured for the user's own Claude Code use must not leak in.
    assert "--strict-mcp-config" in command


def test_tool_schemas_are_written_for_the_bridge_and_env_is_wired(client):
    tools = [{"type": "function", "function": {"name": "list_reminders", "description": "d", "parameters": {}}}]
    captured = {}

    def fake_run(command, **kwargs):
        # The temp workdir is deleted on exit, so read the handoff files while they live.
        config_path = command[command.index("--mcp-config") + 1]
        captured["mcp"] = json.loads(open(config_path, encoding="utf-8").read())
        captured["schema"] = json.loads(open(kwargs["env"]["JARVIS_TOOLS_SCHEMA"], encoding="utf-8").read())
        captured["env"] = kwargs["env"]
        return ok()

    with patch("subprocess.run", side_effect=fake_run):
        client.converse(
            system_prompt="You are Jarvis.", history=[{"role": "user", "content": "hi"}],
            now="t", tz_name="America/New_York", tools=tools,
        )

    assert captured["schema"] == tools
    assert captured["mcp"]["mcpServers"]["jarvis"]["args"][0].endswith("mcp_bridge.py")
    assert captured["env"]["JARVIS_TOOLS_TOKEN"] == "tok"
    assert captured["env"]["JARVIS_USER_ID"] == "7"
    assert captured["env"]["JARVIS_TOOLS_URL"].endswith("/api/tools/call")


def test_image_unlocks_read_only_inside_the_per_turn_workdir(client):
    with patch("subprocess.run", return_value=ok()) as run:
        client.converse(
            system_prompt="You are Jarvis.", history=[{"role": "user", "content": "what is this"}],
            now="t", tz_name="America/New_York", image_bytes=b"\xff\xd8jpegbytes",
        )
    command, kwargs = run.call_args.args[0], run.call_args.kwargs
    denied = command[command.index("--disallowed-tools") + 1].split(",")
    allowed = command[command.index("--allowed-tools") + 1].split(",")
    assert "Read" in allowed and "Read" not in denied
    assert "snapshot.jpg" in command[2]
    # cwd is the throwaway per-turn directory, so --restricted's file confinement means
    # Read can only reach the snapshot itself.
    assert kwargs["cwd"] is not None and "jarvis-claude-" in kwargs["cwd"]


def test_no_image_means_read_stays_denied(client):
    with patch("subprocess.run", return_value=ok()) as run:
        client.converse(
            system_prompt="You are Jarvis.", history=[{"role": "user", "content": "hi"}],
            now="t", tz_name="America/New_York",
        )
    command = run.call_args.args[0]
    assert "Read" in command[command.index("--disallowed-tools") + 1].split(",")
    assert "Read" not in command[command.index("--allowed-tools") + 1].split(",")


def test_chat_never_exposes_tools_even_when_asked(client):
    """engine's confirmation classifier calls chat(); a tool call from there would
    sidestep the very gate it exists to serve."""
    with patch("subprocess.run", return_value=ok("confirm")) as run:
        message = client.chat(
            [{"role": "system", "content": "Classify."}, {"role": "user", "content": "yes do it"}],
            tools=[{"type": "function", "function": {"name": "unlock", "parameters": {}}}],
        )
    command = run.call_args.args[0]
    assert message == {"role": "assistant", "content": "confirm"}
    assert "--mcp-config" not in command
    assert "--allowed-tools" not in command
    assert "Bash" in command[command.index("--disallowed-tools") + 1]


def test_cli_failure_raises_rather_than_returning_a_blank_answer(client):
    with patch("subprocess.run", return_value=FakeCompleted("", returncode=1, stderr="Error: not logged in")):
        with pytest.raises(RuntimeError, match="not logged in"):
            client.converse(
                system_prompt="s", history=[{"role": "user", "content": "hi"}], now="t",
                tz_name="America/New_York",
            )


def test_error_payload_raises(client):
    payload = json.dumps({"result": "usage limit reached", "is_error": True})
    with patch("subprocess.run", return_value=FakeCompleted(payload)):
        with pytest.raises(RuntimeError, match="usage limit"):
            client.converse(
                system_prompt="s", history=[{"role": "user", "content": "hi"}], now="t",
                tz_name="America/New_York",
            )


def test_deny_list_covers_the_documented_escape_tools():
    for name in ("Task", "SendMessage", "ListAgents", "Bash", "WebFetch"):
        assert name in DENIED_TOOLS
