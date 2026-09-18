#!/usr/bin/env python3
"""Real-time pacer between rtl_fm (scan mode) and ffmpeg.

rtl_fm with several -f channels writes NOTHING while every channel is squelched,
so the downstream Icecast mount stalls and phone players time out or show
"buffering" forever. This shim keeps the byte stream flowing at real time:
audio passes straight through the moment rtl_fm opens squelch, and zeros
(silence) fill the gaps while it is hopping.

    rtl_fm -f ... -l <squelch> - | pace.py --rate 12000 | ffmpeg -f s16le -ar 12000 -i - ...

Pure stdlib, no deps. Exits when rtl_fm closes its end so systemd restarts the unit.
"""
import argparse
import os
import queue
import sys
import threading
import time


def _reader(fd, q):
    while True:
        try:
            b = os.read(fd, 16384)
        except OSError:
            b = b""
        if not b:
            q.put(None)
            return
        q.put(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, default=12000, help="samples/sec from rtl_fm")
    ap.add_argument("--bytes-per-sample", type=int, default=2, help="s16le = 2")
    ap.add_argument("--tick", type=float, default=0.05, help="scheduler period, seconds")
    ap.add_argument("--slack", type=float, default=0.3,
                    help="how far behind real time the stream may lag before silence is injected")
    a = ap.parse_args()

    bps = a.rate * a.bytes_per_sample
    slack_bytes = int(a.slack * bps)
    q = queue.Queue()
    threading.Thread(target=_reader, args=(sys.stdin.fileno(), q), daemon=True).start()
    out = sys.stdout.buffer

    start = time.monotonic()
    sent = 0
    eof = False
    try:
        while not eof:
            while True:
                try:
                    b = q.get_nowait()
                except queue.Empty:
                    break
                if b is None:
                    eof = True
                    break
                out.write(b)
                sent += len(b)
            # Keep the stream no further than `slack` behind wall-clock. Real audio
            # normally keeps us within that window on its own; only when rtl_fm goes
            # quiet (hopping between squelched channels) do we fall behind and pad.
            floor = int((time.monotonic() - start) * bps) - slack_bytes
            floor -= floor % a.bytes_per_sample
            if sent < floor:
                pad = floor - sent
                out.write(b"\x00" * pad)
                sent += pad
            out.flush()
            time.sleep(a.tick)
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
