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
