# fm_node — Richmond conventional-FM audio streams (jarvishackrf2)

The "listen tonight" solution that needs no trunk decode: plain-FM receivers piped to a
local Icecast server, so the audio is a URL any phone/browser/VLC can open and Jarvis can
relay to the `jarvisaudio` Pis. Runs on `jarvishackrf2` (192.168.0.161) alongside (but
instead of, for now) the trunk-recorder container, using both RTL-SDRs.

## The streams (open on your phone)

| URL | What |
|---|---|
| `http://192.168.0.161:8000/weather.mp3` | **NOAA Weather Richmond, 162.550 MHz** — continuous, always audio (verified clean, mean −12.9 dB). |
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
- Dongle 0 = weather (continuous, `-l 0`), dongle 1 = scanner (`-l 100` squelch to hop).

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
- Scanner squelch `-l 100` is a starting guess — raise if it camps on noise, lower if it
  never opens. Add/remove `-f` channels from the unit to taste (see `../trunk_node/
  richmond_reference.md` for the full conventional list).
- Jarvis relay: point a `jarvisaudio` Pi (or the phone) at the mount URL; a future Jarvis
  tool can start/stop playback on a remote station or announce Skywarn/weather-alert audio.
