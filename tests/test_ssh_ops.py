"""SSHOpsClient's connection resolution and error handling. run_command's actual
streaming behavior against a real SSH server is exercised indirectly (through
ops_plans.run_plan using a fake client, see test_ops_plans.py) since spinning up a real
sshd for a unit test isn't worth the cost -- what matters here is that host names never
leak a real address/credential the model didn't already know, and that failures come
back as SSHOpsError rather than a raw paramiko exception.
"""
import pytest

from assistant.core.ssh_ops import SSHOpsClient, SSHOpsError


def test_list_hosts_returns_registered_names_sorted():
    client = SSHOpsClient({"zeta": {"host": "z", "user": "u"}, "alpha": {"host": "a", "user": "u"}})
    assert client.list_hosts() == ["alpha", "zeta"]


def test_describe_hosts_surfaces_is_jarvis_host_and_purpose():
    client = SSHOpsClient({
        "jarvisbox": {"host": "127.0.0.1", "user": "swayze", "is_jarvis_host": True, "purpose": "Runs Jarvis."},
        "homeassistant": {"host": "192.168.0.32", "user": "jack", "is_jarvis_host": False, "purpose": "HA server."},
    })
    assert client.describe_hosts() == [
        {"name": "homeassistant", "is_jarvis_host": False, "purpose": "HA server."},
        {"name": "jarvisbox", "is_jarvis_host": True, "purpose": "Runs Jarvis."},
    ]


def test_describe_hosts_defaults_when_a_host_predates_the_metadata_fields():
    """An entry written before is_jarvis_host/purpose existed must not crash or silently
    lie -- it should read as "not known to be a Jarvis host, no purpose on file" rather
    than raising a KeyError."""
    client = SSHOpsClient({"legacy": {"host": "1.2.3.4", "user": "u"}})
    assert client.describe_hosts() == [{"name": "legacy", "is_jarvis_host": False, "purpose": ""}]


def test_unknown_host_raises_with_the_registered_list_not_a_stack_trace():
    client = SSHOpsClient({"simrig": {"host": "simrig.local", "user": "jack"}})
    with pytest.raises(SSHOpsError, match="unknown host 'nope'"):
        client._connect("nope")


def test_unknown_host_error_lists_what_is_actually_registered():
    client = SSHOpsClient({"simrig": {"host": "simrig.local", "user": "jack"},
                          "touch1": {"host": "192.168.0.135", "user": "pi"}})
    with pytest.raises(SSHOpsError, match="simrig.*touch1|touch1.*simrig"):
        client._connect("nope")


def test_empty_registry_says_so_rather_than_an_empty_list():
    client = SSHOpsClient({})
    with pytest.raises(SSHOpsError, match=r"\(none configured\)"):
        client._connect("anything")


def test_a_connection_failure_is_wrapped_as_ssh_ops_error(monkeypatch):
    """A raw paramiko exception (auth failure, unreachable host, etc.) must never
    propagate as-is -- callers (ops_plans.run_plan) catch SSHOpsError specifically."""
    import paramiko

    def fail_connect(self, *a, **kw):
        raise paramiko.AuthenticationException("bad credentials")

    monkeypatch.setattr(paramiko.SSHClient, "connect", fail_connect)
    client = SSHOpsClient({"simrig": {"host": "simrig.local", "user": "jack", "password": "wrong"}})
    with pytest.raises(SSHOpsError, match="could not connect to 'simrig'"):
        client._connect("simrig")


def test_key_path_is_expanded_and_passed_to_connect(monkeypatch):
    captured = {}

    def fake_connect(self, host, **kwargs):
        captured["host"] = host
        captured["kwargs"] = kwargs

    import paramiko
    monkeypatch.setattr(paramiko.SSHClient, "connect", fake_connect)
    client = SSHOpsClient({"simrig": {"host": "simrig.local", "user": "jack", "key_path": "~/.ssh/id_ed25519"}})
    client._connect("simrig")

    assert captured["host"] == "simrig.local"
    assert captured["kwargs"]["username"] == "jack"
    assert "~" not in captured["kwargs"]["key_filename"]  # expanduser() ran


def test_password_auth_is_passed_through_when_no_key_configured(monkeypatch):
    captured = {}

    def fake_connect(self, host, **kwargs):
        captured["kwargs"] = kwargs

    import paramiko
    monkeypatch.setattr(paramiko.SSHClient, "connect", fake_connect)
    client = SSHOpsClient({"touch1": {"host": "192.168.0.135", "user": "pi", "password": "secret"}})
    client._connect("touch1")

    assert captured["kwargs"]["password"] == "secret"
    assert "key_filename" not in captured["kwargs"]


class _FakeChannel:
    """Enough of paramiko's Channel surface for run_command to complete against it."""

    def __init__(self, exit_status=0, stderr=""):
        self._exit_status = exit_status
        self._stderr = stderr
        self.get_pty_called = False
        self.exec_command_calls = []

    def get_pty(self):
        self.get_pty_called = True

    def exec_command(self, command):
        self.exec_command_calls.append(command)

    def recv_ready(self):
        return False

    def recv_stderr_ready(self):
        if self._stderr:
            return True
        return False

    def recv_stderr(self, n):
        out, self._stderr = self._stderr, ""
        return out.encode()

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return self._exit_status


def test_run_command_never_requests_a_pty(monkeypatch):
    """A real, live regression test found that Windows OpenSSH Server silently reports
    exit code 0 for every command run over a PTY channel, regardless of what the command
    actually returned -- confirmed against a real host (a plain `exit 1` read back as
    success). Since ops_plans.run_plan's whole change/test/verify/rollback model depends
    on a trustworthy exit code, get_pty() must never be called here again."""
    import paramiko

    fake_channel = _FakeChannel(exit_status=0)

    class FakeTransport:
        def open_session(self):
            return fake_channel

    monkeypatch.setattr(paramiko.SSHClient, "connect", lambda self, *a, **k: None)
    monkeypatch.setattr(paramiko.SSHClient, "get_transport", lambda self: FakeTransport())

    client = SSHOpsClient({"simrig": {"host": "simrig.local", "user": "jack"}})
    client.run_command("simrig", "echo hi")

    assert fake_channel.get_pty_called is False
    assert fake_channel.exec_command_calls == ["echo hi"]


def test_run_command_reports_the_real_exit_code(monkeypatch):
    import paramiko

    fake_channel = _FakeChannel(exit_status=1)

    class FakeTransport:
        def open_session(self):
            return fake_channel

    monkeypatch.setattr(paramiko.SSHClient, "connect", lambda self, *a, **k: None)
    monkeypatch.setattr(paramiko.SSHClient, "get_transport", lambda self: FakeTransport())

    client = SSHOpsClient({"simrig": {"host": "simrig.local", "user": "jack"}})
    result = client.run_command("simrig", "exit 1")

    assert result == {"ok": False, "host": "simrig", "command": "exit 1", "exit_code": 1, "output": ""}


def test_run_command_surfaces_stderr_in_output(monkeypatch):
    """A real incident: a failing step against jarvisbox came back with an empty
    output and no way to tell why, because paramiko keeps stderr on a separate stream
    from stdout and run_command only ever read stdout. The real error (cmd.exe's "not
    recognized as an internal or external command", from a PowerShell command sent to a
    host whose SSH default shell turned out to be cmd.exe) was there the whole time on
    stderr -- just never read. It must now show up in `output` rather than being
    silently dropped, since that's the only signal a caller (or Jarvis diagnosing a
    failed ops-plan step) has to work with."""
    import paramiko

    fake_channel = _FakeChannel(exit_status=1, stderr="'foo' is not recognized as an internal or external command")

    class FakeTransport:
        def open_session(self):
            return fake_channel

    monkeypatch.setattr(paramiko.SSHClient, "connect", lambda self, *a, **k: None)
    monkeypatch.setattr(paramiko.SSHClient, "get_transport", lambda self: FakeTransport())

    client = SSHOpsClient({"jarvisbox": {"host": "127.0.0.1", "user": "swayze"}})
    result = client.run_command("jarvisbox", "foo")

    assert result["ok"] is False
    assert "not recognized as an internal or external command" in result["output"]
