# fm_node — Richmond conventional-FM audio streams (jarvishackrf2)

The "listen tonight" solution that needs no trunk decode: plain-FM receivers piped to a
local Icecast server, so the audio is a URL any phone/browser/VLC can open and Jarvis can
relay to the `jarvisaudio` Pis. Runs on `jarvishackrf2` (192.168.0.161) alongside (but
instead of, for now) the trunk-recorder container, using both RTL-SDRs.

## The streams (open on your phone)

| URL | What |
|---|---|
| `http://192.168.0.161:8000/weather.mp3` | **NOAA Weather Richmond (KHB37), 162.475 MHz** — continuous, always audio. (2026-09-18: was on 162.550 for two days, which is empty here — a level check can't tell FM noise from voice; verify with a spectrogram, see below.) |
| `http://192.168.0.161:8000/scanner.mp3` | **Richmond Fire/EMS conventional scanner** — squelch-scans 854.0125 (Fire T/A), 854.2375 (PD T/A), 155.340 & 453.975 (Ambulance Authority), 146.88 (Skywarn). Intermittent — these are talkaround/backup channels, so mostly quiet with real traffic when it happens. |
| `http://192.168.0.161:8000/status.xsl` | Icecast status page (lists mounts, listener counts). |

`jarvishackrf2.local:8000` works too where mDNS resolves.

## How it's built

- **Icecast 2.4.4** (`/etc/icecast2/icecast.xml`, port 8000; source pw `jarvisradio`,
  admin `admin`/`jarvisadmin` — LAN-only, change if exposed). Needs a `<changeowner>`
  directive or it refuses to run under the init script. Enable via `ENABLE=true` in
  `/etc/default/icecast2`.
- Two systemd services here (`jarvis-fm-weather.service`, `jarvis-fm-scanner.service`):
  `rtl_fm -> ffmpeg (mp3, 32k) -> icecast`. **`-legacy_icecast 1` is required** — ffmpeg's
  default icecast PUT method is rejected by Icecast 2.4; legacy uses the SOURCE method.
- Dongle 0 = weather (continuous, `-l 0`), dongle 1 = scanner (`-l 900` squelch to hop).
- `pace.py` sits between rtl_fm and ffmpeg on the scanner: in scan mode rtl_fm writes
  *nothing* while every channel is squelched, which stalls the Icecast mount and makes
  phone players time out. The pacer passes audio straight through and pads real-time
  silence in the gaps so the mount never stalls. Pure stdlib; lives at
  `/home/pi/fm_node/pace.py` on the Pi.

## EAS / SAME alert watcher (`eas_watch.py`, service `jarvis-eas-watch`)

NOAA precedes every warning with a SAME digital header (AFSK, sent 3x). `eas_watch.py`
runs `ffmpeg (mount -> 22.05 kHz raw) | multimon-ng -a EAS`, parses `ZCZC-WXR-TOR-051087+0045-...`
into event/severity/counties/expiry and appends JSON lines to `/home/pi/fm_node/eas_events.jsonl`.
`EAS_MY_FIPS` in the unit lists the home counties (Richmond city, Henrico, Chesterfield,
Hanover) -> `affects_me`. Validated 2026-09-18 with a synthesized SAME burst through the exact
mp3 chain. The Jarvis box (`assistant/radio_main.py`) tails this file over SSH every 60 s and
pushes warnings; see `docs/radio-awareness-design.md`. Self-test: `python3 eas_watch.py --parse
"<header>"`, or point `EAS_STREAM_URL` at a wav/mp3 file.

## Deploy / redeploy

```sh
scp fm_node/*.service pi@192.168.0.161:/tmp/
ssh pi@192.168.0.161 'sudo cp /tmp/jarvis-fm-*.service /etc/systemd/system/ && \
  sudo systemctl daemon-reload && \
  sudo systemctl enable --now jarvis-fm-weather jarvis-fm-scanner'
```
(Icecast config + install steps are in memory `project_trunk_scanner_node.md`.)

## Notes / tuning

- **Dongle contention:** these use both RTL-SDRs, so the trunk-recorder container
  (`jarvis-trunk`) is **stopped** while these run. Re-starting it needs a dongle freed.
- Scanner squelch: **`-l 900`**, calibrated 2026-09-18 on the real dongle at gain 45. Idle
  noise on the worst channel (155.340, VHF) squelches between 400 and 600; 854 MHz idles
  below 100; NOAA 162.475 still opens at 4000. `-l 100` (the original guess) let VHF
  noise through continuously. Recalibrate if gain or antenna changes: stop the scanner
  unit and run `rtl_fm -d 1 -f <idle freq> -l N ... | wc -c` for a few N — rtl_fm emits
  zero bytes when squelched, so the first N that gives 0 bytes is the floor.
- **Verifying a stream actually has audio:** a level/RMS check is useless (open-squelch FM
  noise measures the same −12 dB as speech). Render a spectrogram instead:
  `ffmpeg -t 20 -i http://127.0.0.1:8000/weather.mp3 -c copy /tmp/s.mp3 && ffmpeg -i /tmp/s.mp3 -lavfi showspectrumpic=s=1200x400:legend=1 /tmp/s.png`
  — voice shows harmonic bands under ~3 kHz with pauses; noise is a flat uniform wash. Add/remove `-f` channels from the unit to taste (see `../trunk_node/
  richmond_reference.md` for the full conventional list).
- Jarvis relay: point a `jarvisaudio` Pi (or the phone) at the mount URL; a future Jarvis
  tool can start/stop playback on a remote station or announce Skywarn/weather-alert audio.
