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

    def __init__(self, exit_status=0):
        self._exit_status = exit_status
        self.get_pty_called = False
        self.exec_command_calls = []

    def get_pty(self):
        self.get_pty_called = True

    def exec_command(self, command):
        self.exec_command_calls.append(command)

    def recv_ready(self):
        return False

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
