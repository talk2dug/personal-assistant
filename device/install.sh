#!/usr/bin/env bash
# Installs the Jarvis voice terminal on a Raspberry Pi. Designed to be run identically on
# every unit regardless of its microphone/speaker/Pi model -- jarvis_device.py itself
# already auto-detects and adapts to whatever audio hardware is actually plugged in
# (resolve_audio_devices/pick_capture_rate/Speaker._resample), so this script's only job
# is to get the right software in place, not to know anything about a specific device's
# hardware.
#
# Safe to re-run: every step either checks first or is naturally idempotent (apt install
# of an installed package, systemctl enable of an enabled unit). Re-running after copying
# a newer jarvis_device.py is how a terminal gets updated.
#
# Usage (from this directory, after deploy_to_pi.py has copied it here):
#   ./install.sh <device_id> <device_name> [server_url]
#
# device_id must be unique per terminal (used in the server URL path and its own
# state key) and stick to [a-z0-9_-] since it ends up in a systemd unit name.
set -euo pipefail

DEVICE_ID="${1:?Usage: install.sh <device_id> <device_name> [server_url]}"
DEVICE_NAME="${2:?Usage: install.sh <device_id> <device_name> [server_url]}"
SERVER_URL="${3:-http://192.168.0.148:8080}"

if [[ ! "$DEVICE_ID" =~ ^[a-z0-9_-]+$ ]]; then
    echo "device_id must be lowercase letters, digits, _ or - (got: $DEVICE_ID)" >&2
    exit 1
fi

INSTALL_DIR="/opt/jarvis-device"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="$(whoami)"

# Not every account this runs under has passwordless sudo -- confirmed directly: the
# Raspberry Pi OS default `pi` user does, but a plain Ubuntu install does not, and
# without this every sudo call below would silently hang forever waiting on a TTY
# password prompt nobody can answer over a non-interactive SSH command. `sudo -n true`
# succeeds instantly on an account that doesn't need one, so the common case (Pi) pays
# no cost; only an account that actually needs a password requires the extra file.
if sudo -n true 2>/dev/null; then
    sudo() { command sudo "$@"; }
else
    SUDO_PASSWORD_FILE="$SCRIPT_DIR/sudo_password.txt"
    if [[ ! -f "$SUDO_PASSWORD_FILE" ]]; then
        echo "sudo on this account needs a password, and deploy_to_pi.py should have" >&2
        echo "provided one at $SUDO_PASSWORD_FILE -- see its --sudo-password option." >&2
        exit 1
    fi
    echo "==> this account's sudo needs a password; using the one deploy_to_pi.py provided"
    # The "[sudo] password for x:" prompt line still lands in the log -- harmless noise,
    # left visible rather than swallowed, since blanket-hiding stderr here would just as
    # easily hide a real apt/systemctl error underneath it.
    sudo() { command sudo -S "$@" < "$SUDO_PASSWORD_FILE"; }
fi

echo "==> Installing Jarvis terminal '$DEVICE_ID' ($DEVICE_NAME) into $INSTALL_DIR"

# --- system packages ---------------------------------------------------------------
# portaudio19-dev: sounddevice's build dependency (PortAudio headers).
# python3-dev: needed to build the few wheels piwheels doesn't ship prebuilt.
# libatlas-base-dev: numpy/scipy's BLAS/LAPACK backend on ARM -- without it numpy still
#   imports but is dramatically slower, which matters on the audio callback's 80ms budget.
echo "==> apt packages"
sudo apt-get update -qq
sudo apt-get install -y -qq portaudio19-dev python3-dev python3-venv libatlas-base-dev alsa-utils

# `pi` (or whichever user this runs as) needs to be in the audio group to open ALSA
# capture/playback devices without root. Harmless to re-run on a user already in it.
if ! groups "$RUN_USER" | grep -qw audio; then
    echo "==> adding $RUN_USER to the audio group (takes effect next login)"
    sudo usermod -aG audio "$RUN_USER"
fi

# Route ALSA through PulseAudio when Pulse is actually running the show. Confirmed
# directly on real hardware (a laptop with a single shared codec): with no routing
# config, PortAudio's raw ALSA calls fight PulseAudio for the same card -- dsnoop (the
# ALSA layer that lets multiple streams share one capture device) failed with "unable to
# open slave" because Pulse already had it, and the moment a second stream (the wake
# chime) opened while the capture stream's callback was mid-poll, PortAudio's ALSA
# backend hit an internal assertion and aborted the whole process (SIGABRT). Pointing
# ALSA's default at Pulse -- what pulseaudio-managed desktops normally ship configured
# out of the box, which this "minimal" one didn't -- fixes it at the root: Pulse is
# built to mix multiple concurrent streams onto one card, dmix/dsnoop are not reliably.
# A Pi/other unit with no PulseAudio running is untouched by this.
if pgrep -x pulseaudio >/dev/null 2>&1; then
    if [[ -f "$HOME/.asoundrc" ]]; then
        echo "==> PulseAudio is running but ~/.asoundrc already exists -- leaving it alone."
        echo "    If audio then crashes the moment a chime tries to play while the mic is"
        echo "    open, that file is probably why; point its default pcm/ctl at 'pulse'."
    else
        echo "==> PulseAudio detected -- routing ALSA's default device through it"
        cat > "$HOME/.asoundrc" <<'EOF'
pcm.!default {
    type pulse
}
ctl.!default {
    type pulse
}
EOF
    fi
fi

# --- install location --------------------------------------------------------------
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$RUN_USER:$RUN_USER" "$INSTALL_DIR"
cp "$SCRIPT_DIR/jarvis_device.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

# --- python environment --------------------------------------------------------------
echo "==> python venv + dependencies"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
# requirements.txt pins numpy<2 ahead of openwakeword deliberately: tflite-runtime's
# wheel is built against NumPy's 1.x C-ABI and breaks (an obscure ImportError, not an
# obvious version-mismatch message) on NumPy 2 -- this has hit this project twice
# already. Confirmed directly on a fresh Bookworm/Python 3.11 image that a plain install
# with that pin in place resolves cleanly (real tflite-runtime wheel exists for this
# platform via piwheels) with no need for --no-deps surgery.
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# openwakeword ships with NO model files in the package itself -- confirmed by hitting
# this directly: the wheel installs cleanly, imports cleanly, and then crash-loops on
# first run because openwakeword/resources/models/ doesn't exist. This one-time download
# (a few MB) is mandatory, not optional, and belongs here rather than left for
# jarvis_device.py to discover at runtime.
echo "==> downloading wake word models"
"$INSTALL_DIR/.venv/bin/python3" -c "from openwakeword.utils import download_models; download_models()" 2>&1 | tail -3

# --- device config ---------------------------------------------------------------
# The device API key is shared across every terminal (assistant/web/routes/devices.py
# checks one static cfg.device_api_key, not a per-device secret) and is pushed here as
# its own file by deploy_to_pi.py rather than passed as a script argument, so it never
# ends up sitting in this shell's history.
API_KEY_FILE="$SCRIPT_DIR/api_key.txt"
if [[ ! -f "$API_KEY_FILE" ]]; then
    echo "missing $API_KEY_FILE -- deploy_to_pi.py should have written it" >&2
    exit 1
fi
API_KEY="$(cat "$API_KEY_FILE")"

# input_device/output_device are left null (system default) deliberately: hardware
# varies per unit, and resolve_audio_devices() in jarvis_device.py already falls back to
# the system default and logs what it found. A fresh unit should just work; naming a
# specific card is something to add later only if a Pi has more than one candidate
# input/output and the wrong one is being picked.
cat > "$INSTALL_DIR/device.json" <<EOF
{
  "server": "$SERVER_URL",
  "device_id": "$DEVICE_ID",
  "device_name": "$DEVICE_NAME",
  "api_key": "$API_KEY",
  "wake_model": "hey_jarvis",
  "wake_threshold": 0.5,
  "inference_framework": "tflite",
  "vad_floor": 0.006,
  "vad_margin": 2.5,
  "input_device": null,
  "output_device": null,
  "chime": true
}
EOF
chmod 600 "$INSTALL_DIR/device.json"    # contains the shared device_api_key
rm -f "$API_KEY_FILE"                   # don't leave a second copy of the key lying around

# --- systemd service ---------------------------------------------------------------
echo "==> systemd service"
# Written to a regular file first, not piped straight into `sudo tee`: the sudo wrapper
# above feeds the sudo password in on stdin for accounts that need one, and a pipe into
# sudo's own stdin here would collide with that -- `sudo cp` needs no stdin of its own,
# so there is nothing for the two to fight over.
sed -e "s|%USER%|$RUN_USER|g" -e "s|%INSTALL_DIR%|$INSTALL_DIR|g" \
    "$SCRIPT_DIR/jarvis-device.service.template" > /tmp/jarvis-device.service
sudo cp /tmp/jarvis-device.service /etc/systemd/system/jarvis-device.service
rm -f /tmp/jarvis-device.service
sudo systemctl daemon-reload
sudo systemctl enable jarvis-device.service
sudo systemctl restart jarvis-device.service

echo "==> checking for a capture device"
if ! arecord -l 2>/dev/null | grep -q "^card"; then
    echo "    WARNING: no capture device detected (arecord -l shows none)."
    echo "    The service will still start, but it cannot hear a wake word until a"
    echo "    USB microphone is plugged in. Re-run 'sudo systemctl restart jarvis-device'"
    echo "    after plugging one in -- no reinstall needed."
fi

# --- kiosk display (only if this unit has one) --------------------------------------
# Not every terminal has a screen -- some are audio-only satellites around the house --
# so this is conditional on a desktop actually being present rather than assumed. Skips
# cleanly and says why on an audio-only unit instead of failing.
#
# --password-store=basic: without it, Chromium's first launch tries to talk to the
# session's keyring (via libsecret) to store its credential store and blocks on a
# "create a password for a new keyring" dialog nobody is there to answer on a kiosk
# screen with no keyboard. This tells it to use its own plain internal store instead
# of the OS keyring at all, so nothing ever prompts.
if command -v chromium >/dev/null 2>&1 && [[ "$(systemctl get-default)" == "graphical.target" ]]; then
    echo "==> kiosk display"

    # Raspberry Pi OS's autologin never enters a password, so PAM never gets one to
    # auto-unlock or auto-create a login keyring with (that normally rides along with a
    # real interactive login). gnome-keyring-daemon starts anyway (it's launched
    # directly, not through the XDG autostart entries under /etc/xdg/autostart, which
    # are scoped to OnlyShowIn=GNOME/Unity/MATE and would never fire under labwc), finds
    # no default keyring on disk, and the first thing that touches the Secret Service
    # gets an interactive "create a password for a new keyring" dialog -- with no
    # keyboard attached to answer it. Confirmed directly: --password-store=basic below
    # stops Chromium asking, but the dialog still appeared, meaning some other component
    # hit the keyring first. Pre-seeding an already-"created", blank-password default
    # keyring removes the interactive path entirely regardless of what asks first.
    if [[ ! -f "$HOME/.local/share/keyrings/default" ]]; then
        echo "==> pre-seeding an unlocked default keyring (kiosk has no keyboard to answer that prompt)"
        mkdir -p "$HOME/.local/share/keyrings"
        cat > "$HOME/.local/share/keyrings/Default_keyring.keyring" <<'KEYRING_EOF'
[keyring]
display-name=Default
ctime=0
mtime=0
lock-on-idle=false
lock-after=false
KEYRING_EOF
        echo -n "Default_keyring" > "$HOME/.local/share/keyrings/default"
        chmod 600 "$HOME/.local/share/keyrings/Default_keyring.keyring" "$HOME/.local/share/keyrings/default"
    fi

    mkdir -p "$HOME/.config/autostart"
    # web/src/pages/Device.jsx: a terminal's screen is unauthenticated and identifies
    # itself entirely from these two URL params (no login, nobody to sign a shelf unit
    # in) -- this is the same URL scheme app.jsx routes on /device to.
    KIOSK_URL="${SERVER_URL}/device?id=${DEVICE_ID}&key=${API_KEY}"
    KIOSK_PROFILE="$HOME/.config/jarvis-kiosk-chromium"

    # The actual command lives in its own script, not inline in Exec=. The Desktop
    # Entry spec has its own quoting rules for the Exec value (no shell in between, so
    # no shell-style line continuation -- a multi-line Exec here previously parsed as
    # broken/truncated: chromium launched but landed on its own new-tab page instead of
    # the target URL). A real bash script with the URL in a genuine double-quoted
    # string sidesteps that question entirely rather than betting on how a given
    # implementation's Exec-line splitter treats '&'/'?' in a raw, unquoted token.
    cat > "$HOME/.config/jarvis-kiosk-launch.sh" <<EOF
#!/usr/bin/env bash
exec chromium --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble \\
    --disable-translate --no-first-run --check-for-update-interval=31536000 \\
    --overscroll-history-navigation=0 --disable-pinch --password-store=basic \\
    --user-data-dir="$KIOSK_PROFILE" "$KIOSK_URL"
EOF
    chmod +x "$HOME/.config/jarvis-kiosk-launch.sh"

    # Which autostart mechanism actually fires depends on the window manager, and that
    # varies enough between units that it can't be assumed. Confirmed directly: XDG
    # autostart (~/.config/autostart/*.desktop) works under labwc (Raspberry Pi OS's
    # default), but a bare Openbox session -- exactly what a "minimal, kiosk-ready"
    # machine turned out to be running -- never reads that directory at all; Openbox has
    # its own autostart file and nothing else honours it.
    #
    # That Openbox box already had a hand-prepared kiosk autostart script with screen-
    # blanking disabled, cursor-hiding, and a marked placeholder section for exactly
    # this ("AI ASSISTANT REMOTE CLIENT GOES HERE") -- so when that's what's there, this
    # plugs into it rather than fighting it with a second, competing autostart path the
    # rest of that file's setup wouldn't be part of.
    OB_AUTOSTART="$HOME/.config/openbox/autostart"
    if [[ -f "$OB_AUTOSTART" ]]; then
        echo "==> Openbox session detected -- adding to its existing autostart script"
        if ! grep -qF "jarvis-kiosk-launch.sh" "$OB_AUTOSTART"; then
            {
                echo ""
                echo "# --- Jarvis kiosk client (added by device/install.sh) ---"
                echo "\"$HOME/.config/jarvis-kiosk-launch.sh\" &"
            } >> "$OB_AUTOSTART"
        fi
    else
        mkdir -p "$HOME/.config/autostart"
        cat > "$HOME/.config/autostart/jarvis-kiosk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Jarvis Kiosk
Exec=$HOME/.config/jarvis-kiosk-launch.sh
X-GNOME-Autostart-enabled=true
EOF
    fi
    echo "    autostart written -- takes effect on next graphical login/reboot"
else
    echo "==> no desktop environment detected; skipping kiosk display (audio-only unit)"
fi

sleep 3
echo "==> service status"
sudo systemctl --no-pager status jarvis-device.service || true

# Last use of sudo in this script -- don't leave a second copy of a real account
# password sitting on disk any longer than the install actually needed it.
[[ -n "${SUDO_PASSWORD_FILE:-}" ]] && rm -f "$SUDO_PASSWORD_FILE"

echo
echo "==> done. Follow logs with: journalctl -u jarvis-device -f"
