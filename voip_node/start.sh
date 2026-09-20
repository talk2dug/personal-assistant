#!/bin/bash
# Start the Jarvis VoIP audio bridge for an attended live-call test.
# Loads the ALSA loopback, then runs the orchestrator which launches baresip.
set -e
sudo modprobe snd-aloop 2>/dev/null || true
exec /usr/bin/python3 -u "$HOME/voip-bridge/bridge.py" "$@"
