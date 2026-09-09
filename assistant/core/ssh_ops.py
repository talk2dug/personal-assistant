"""SSH command execution for the dev-team ops-plan workflow.

Consolidates the two paramiko call sites this project already had (device/deploy_to_pi.py's
stream_exec, assistant/core/media_scan.py's simpler exec_command variant) into one client,
built from deploy_to_pi.py's pattern since it actually streams output and returns a real
exit code rather than blocking silently on a long-running command.

Deliberately NOT a chat-facing tool in its own right. Every other integration in this
codebase that reaches outside Jarvis's own database is either safe-by-nature (search,
read-only lookups) or individually gated behind pending_actions -- but the owner's explicit
requirement for server changes was a *plan* he approves once, not a yes/no per command (see
ops_plans.py). Exposing run_command directly as a tool would let a model route around that
by just calling it repeatedly. So this class has no call_tool() dispatch method; it is only
ever driven by ops_plans.run_plan(), after the owning plan has already been approved.

Hosts are referred to by a short name registered in config.json's ssh_hosts, never a raw
IP/credential a model supplies -- same principle as every other credential-injecting
wrapper here (CCXT, git_ops): the real connection details are injected server-side.
"""
import time
from pathlib import Path

import paramiko


class SSHOpsError(Exception):
    pass


class SSHOpsClient:
    def __init__(self, hosts: dict):
        self.hosts = hosts  # {name: {"host", "user", "key_path"?, "password"?}}

    def list_hosts(self) -> list[str]:
        return sorted(self.hosts)

    def _connect(self, host_name: str) -> paramiko.SSHClient:
        cfg = self.hosts.get(host_name)
        if cfg is None:
            raise SSHOpsError(f"unknown host {host_name!r}; known hosts: {', '.join(sorted(self.hosts)) or '(none configured)'}")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {"username": cfg["user"], "timeout": 15}
        if cfg.get("key_path"):
            kwargs["key_filename"] = str(Path(cfg["key_path"]).expanduser())
        if cfg.get("password"):
            kwargs["password"] = cfg["password"]
        try:
            client.connect(cfg["host"], **kwargs)
        except Exception as e:
            raise SSHOpsError(f"could not connect to {host_name!r} ({cfg['host']}): {e}")
        return client

    def run_command(self, host_name: str, command: str, timeout: int = 120) -> dict:
        """Runs one command on a registered host, streaming semantics mirrored from
        deploy_to_pi.py's stream_exec: poll recv_ready()/exit_status_ready() rather than
        a single blocking exec_command(), so a long install doesn't look indistinguishable
        from a hang, and a real exit code comes back rather than just "did it print
        anything." Raises SSHOpsError on connection failure or timeout; a real but
        nonzero exit code is returned normally (ok=False), not raised, since that's a
        legitimate command result the caller/runner needs to see and react to."""
        client = self._connect(host_name)
        try:
            transport = client.get_transport()
            channel = transport.open_session()
            # Deliberately NOT channel.get_pty() -- deploy_to_pi.py's stream_exec (which
            # this was originally mirrored from) requests one, but confirmed live against
            # a real Windows OpenSSH Server host (jarvisbox) that doing so makes
            # recv_exit_status() always come back 0, regardless of the command's real
            # exit code -- `exit 1`, a thrown exception, and a failed cmd.exe command all
            # silently read back as success. The identical command without a PTY reports
            # its real exit code correctly. Since ops_plans.run_plan's entire
            # change/test/verify/rollback logic depends on exit_code being trustworthy,
            # a PTY here would make a broken verify step silently report "succeeded" --
            # exactly the class of silent-false-positive this project is trying hardest
            # to eliminate elsewhere. Streaming output (recv_ready/recv below) works
            # identically without one.
            channel.exec_command(command)
            chunks, err_chunks = [], []
            start = time.time()
            while True:
                while channel.recv_ready():
                    chunks.append(channel.recv(4096).decode(errors="replace"))
                # A remote shell's error text (e.g. cmd.exe's "not recognized as an
                # internal or external command") arrives on the stderr stream, which
                # paramiko keeps separate from stdout unless combined -- a real incident
                # showed a failing step reporting a blank, uninformative "" output with
                # no way to tell why, because only stdout was ever read here. Merged into
                # the same `output` field (labeled) rather than a new field, so every
                # existing consumer of a step's output sees it automatically.
                while channel.recv_stderr_ready():
                    err_chunks.append(channel.recv_stderr(4096).decode(errors="replace"))
                if (channel.exit_status_ready() and not channel.recv_ready()
                        and not channel.recv_stderr_ready()):
                    break
                if time.time() - start > timeout:
                    raise SSHOpsError(f"command timed out after {timeout}s on {host_name!r}: {command!r}")
                time.sleep(0.1)
            exit_code = channel.recv_exit_status()
            output = "".join(chunks)
            if err_chunks:
                output += ("\n" if output else "") + "[stderr] " + "".join(err_chunks)
            return {
                "ok": exit_code == 0, "host": host_name, "command": command,
                "exit_code": exit_code, "output": output,
            }
        finally:
            client.close()
