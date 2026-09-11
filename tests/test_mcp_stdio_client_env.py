"""StdioMCPClient must not leak the calling process's own PYTHONPATH/PYTHONHOME into a
spawned MCP server -- real incident: jarvis-core.service's Environment registry value
sets PYTHONPATH to the main .venv's site-packages (pythonservice.exe needs it to find
pywin32), and that got inherited verbatim into kroger-mcp.exe, which lives in its own,
deliberately different .venv-kroger, crashing it on startup under the real service every
time (MCPError: Connection closed) while working fine interactively, where that override
was never set.
"""
from assistant.core.mcp_stdio_client import StdioMCPClient


def test_pythonpath_is_stripped_from_the_child_environment(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", r"C:\wrong\venv\site-packages")
    client = StdioMCPClient("some-server.exe")
    assert "PYTHONPATH" not in client.env


def test_pythonhome_is_stripped_from_the_child_environment(monkeypatch):
    monkeypatch.setenv("PYTHONHOME", r"C:\wrong\venv")
    client = StdioMCPClient("some-server.exe")
    assert "PYTHONHOME" not in client.env


def test_other_real_environment_variables_still_pass_through(monkeypatch):
    monkeypatch.setenv("PATH", r"C:\some\real\path")
    client = StdioMCPClient("some-server.exe")
    assert client.env["PATH"] == r"C:\some\real\path"


def test_an_explicit_env_kwarg_still_wins_over_the_inherited_environment(monkeypatch):
    monkeypatch.setenv("KROGER_CLIENT_ID", "wrong")
    client = StdioMCPClient("some-server.exe", env={"KROGER_CLIENT_ID": "right"})
    assert client.env["KROGER_CLIENT_ID"] == "right"
