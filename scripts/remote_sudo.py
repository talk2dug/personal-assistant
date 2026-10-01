"""Run a shell script with sudo on a registered host, streaming its output.

    python scripts/remote_sudo.py tism script.sh
    python scripts/remote_sudo.py tism -c "apt-get update"

Uses passwordless sudo where the box has it, otherwise `sudo -S` with the scan password
fed on stdin (never on the command line). The script is sent over stdin too, so nothing
sensitive or long ends up in a process listing.
"""
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from inventory_drives import CFG, host_list, ssh_connect  # noqa: E402


def run(host: str, script: str, timeout: int = 3600) -> int:
    h = {x["name"]: x for x in host_list()}[host]
    c = ssh_connect(h)
    try:
        nopass = c.exec_command("sudo -n true 2>/dev/null && echo yes")[1].read().decode().strip() == "yes"
        # The script travels as a heredoc-free base64 blob so quoting never matters.
        import base64
        blob = base64.b64encode(script.encode()).decode()
        inner = f"echo {blob} | base64 -d | bash -e"
        cmd = (f"sudo -n bash -c {shlex.quote(inner)}" if nopass
               else f"sudo -S -p '' bash -c {shlex.quote(inner)}")
        chan = c.get_transport().open_session()
        chan.set_combine_stderr(True)
        chan.exec_command(cmd)
        if not nopass:
            chan.sendall((CFG.scan_ssh_password + "\n").encode())
        chan.shutdown_write()
        start = time.time()
        while True:
            if chan.recv_ready():
                sys.stdout.write(chan.recv(1 << 16).decode("utf-8", "replace"))
                sys.stdout.flush()
            elif chan.exit_status_ready():
                break
            elif time.time() - start > timeout:
                print(f"\n[remote_sudo] timed out after {timeout}s")
                return 124
            else:
                time.sleep(0.05)
        while chan.recv_ready():
            sys.stdout.write(chan.recv(1 << 16).decode("utf-8", "replace"))
        return chan.recv_exit_status()
    finally:
        c.close()


if __name__ == "__main__":
    host = sys.argv[1]
    script = sys.argv[3] if sys.argv[2] == "-c" else Path(sys.argv[2]).read_text(encoding="utf-8")
    code = run(host, script)
    print(f"\n[remote_sudo] exit {code}")
    sys.exit(code)
