#!/usr/bin/env python3
"""Pushes the Jarvis voice terminal to a fresh Raspberry Pi and installs it.

This is the operator side of the two-part installer: install.sh runs ON the Pi and is
what actually makes the terminal work; this script exists so that adding a new terminal
is one command from the laptop, whatever hardware that particular Pi happens to have --
jarvis_device.py itself already adapts to the microphone/speaker it finds at runtime, so
nothing here needs to know or ask about hardware.

Usage:
    python device/deploy_to_pi.py <pi-host> <device_id> <device_name>
    python device/deploy_to_pi.py 192.168.0.137 touch3 "Touch3" --ssh-password '...'

The device API key is read from config.json (device_api_key) rather than passed on the
command line, so it never sits in this shell's history on either end -- it's written to
a short-lived file on the Pi that install.sh deletes once it's copied into device.json.
"""
import argparse
import json
import pathlib
import shlex
import sys
import time

import paramiko

# Windows' console defaults to cp1252, which can't encode characters a Pi's own tools
# print (e.g. update-alternatives' '->'). Remote output is arbitrary and out of this
# script's control, so force utf-8 here rather than have an install that's actually
# succeeding get killed by a print() call.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_SSH_USER = "pi"
DEFAULT_SSH_PASSWORD = "UUnv9njxg123"     # this project's standing Pi credential
DEFAULT_SERVER_URL = "http://192.168.0.148:8080"

DEVICE_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = DEVICE_DIR.parent
REMOTE_DIR = "jarvis-device-install"


def load_device_api_key() -> str:
    cfg = json.loads((REPO_ROOT / "config.json").read_text())
    key = cfg.get("device_api_key")
    if not key:
        raise SystemExit("config.json has no device_api_key set -- every terminal shares "
                         "one key (assistant/web/routes/devices.py checks a single "
                         "cfg.device_api_key), so it must exist before deploying any device.")
    return key


def stream_exec(client: paramiko.SSHClient, command: str) -> int:
    """Runs a remote command, printing its output as it arrives rather than waiting for
    it to finish -- install.sh does apt/pip installs that take real minutes, and silence
    that long looks identical to a hang."""
    transport = client.get_transport()
    channel = transport.open_session()
    channel.get_pty()
    channel.exec_command(command)
    while True:
        while channel.recv_ready():
            sys.stdout.write(channel.recv(4096).decode(errors="replace"))
            sys.stdout.flush()
        if channel.exit_status_ready() and not channel.recv_ready():
            break
        time.sleep(0.1)
    return channel.recv_exit_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", help="Pi's IP or hostname")
    parser.add_argument("device_id", help="unique id, e.g. touch3 (lowercase, digits, _ or -)")
    parser.add_argument("device_name", help="display name shown in the office UI, e.g. Touch3")
    parser.add_argument("--server", default=DEFAULT_SERVER_URL, help=f"Jarvis server URL (default {DEFAULT_SERVER_URL})")
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER)
    parser.add_argument("--ssh-password", default=DEFAULT_SSH_PASSWORD)
    parser.add_argument("--sudo-password", default=None,
                        help="defaults to --ssh-password; only needed on accounts without "
                             "passwordless sudo (confirmed: Raspberry Pi OS's default 'pi' "
                             "user has it, a plain Ubuntu account does not)")
    args = parser.parse_args()

    api_key = load_device_api_key()

    print(f"==> connecting to {args.host} as {args.ssh_user}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(args.host, username=args.ssh_user, password=args.ssh_password, timeout=15)
    except Exception as e:
        print(f"could not connect: {e}", file=sys.stderr)
        return 1

    # Only written when actually needed, checked here rather than always pushed: a
    # passwordless-sudo account (the Pi default) that never reads sudo_password.txt would
    # otherwise keep an unused real account password sitting on disk indefinitely, since
    # install.sh only deletes the file on the path that actually consumes it.
    _, out, _ = client.exec_command("sudo -n true", timeout=10)
    needs_sudo_password = out.channel.recv_exit_status() != 0

    sftp = client.open_sftp()
    try:
        sftp.mkdir(REMOTE_DIR)
    except IOError:
        pass  # already exists -- fine, this is meant to be re-run

    print("==> copying installer files")
    for name in ("jarvis_device.py", "requirements.txt", "install.sh", "jarvis-device.service.template"):
        local = DEVICE_DIR / name if name != "jarvis-device.service.template" else REPO_ROOT / "deploy" / name
        sftp.put(str(local), f"{REMOTE_DIR}/{name}")
    # Written fresh each deploy and deleted by install.sh once consumed -- it should never
    # persist as a second copy of the shared secret alongside device.json.
    with sftp.open(f"{REMOTE_DIR}/api_key.txt", "w") as f:
        f.write(api_key)
    sftp.chmod(f"{REMOTE_DIR}/api_key.txt", 0o600)
    if needs_sudo_password:
        print("==> this account's sudo needs a password; pushing one for install.sh to use")
        sudo_password = args.sudo_password or args.ssh_password
        with sftp.open(f"{REMOTE_DIR}/sudo_password.txt", "w") as f:
            f.write(sudo_password)
        sftp.chmod(f"{REMOTE_DIR}/sudo_password.txt", 0o600)
    sftp.close()

    print(f"==> running install.sh {args.device_id} {args.device_name!r} {args.server}")
    # shlex.quote, not json.dumps -- this string is interpreted by the remote shell, and
    # JSON's escaping rules aren't its escaping rules (a name with an apostrophe would
    # otherwise either break the command or, worse, be interpreted as shell syntax).
    remote_cmd = (
        f"chmod +x {REMOTE_DIR}/install.sh && cd {REMOTE_DIR} && "
        f"./install.sh {shlex.quote(args.device_id)} {shlex.quote(args.device_name)} "
        f"{shlex.quote(args.server)}"
    )
    code = stream_exec(client, remote_cmd)
    client.close()

    if code == 0:
        print(f"\n==> {args.device_id} installed and running.")
    else:
        print(f"\n==> install.sh exited {code} -- see output above.", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
