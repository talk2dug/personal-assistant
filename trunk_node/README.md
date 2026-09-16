# trunk_node — Richmond emergency-services scanner (jarvishackrf2)

Runs on `jarvishackrf2` (Pi 4B 8GB, 192.168.0.161), two RTL-SDRs (serials `1000`/`1001`).
Follows the **Capital Region Radio System** (P25 Phase II, Richmond/Henrico/Chesterfield).
**Police/sheriff/jails are encrypted** and skipped; **Fire & EMS are in the clear** — the target.

Deployed on the Pi at `/home/pi/trunk/` (config.json, talkgroups.csv, media/) as a Docker
container `jarvis-trunk` (`ghcr.io/robotastic/trunk-recorder`, `--restart unless-stopped`,
so it comes back on boot).

## Deploy / redeploy

```sh
# from this repo:
scp trunk_node/config.json trunk_node/talkgroups.csv pi@192.168.0.161:/home/pi/trunk/
ssh pi@192.168.0.161 'sudo docker rm -f jarvis-trunk 2>/dev/null; \
  sudo docker run -d --restart unless-stopped --name jarvis-trunk --privileged \
    -v /dev/bus/usb:/dev/bus/usb -v /home/pi/trunk:/app \
    ghcr.io/robotastic/trunk-recorder:latest'
ssh pi@192.168.0.161 'sudo docker logs -f jarvis-trunk'   # watch it lock + record
```

## State as of 2026-09-16 (honest)

WORKS: both dongles addressed by serial, control channel **857.0875 MHz locks**,
trunk-recorder decodes the **system identity** (ID 2AA, WACN BEE00, NAC 2A3 — matches
RadioReference). Hardware, drivers, dual-SDR addressing, control-channel freq and system
config are all proven correct.

NOT YET: it decodes the periodic system broadcasts but is **not catching per-call grants**
(0 recordings), so no Fire/EMS audio yet. This is the known-hard part: **P25 Phase II
simulcast decode** on RTL-SDRs. Tried gain 40 and 28 (no change), encrypted shown/hidden
(no grants either way), no decode-rate warnings.

## Tuning plan (do against DAYTIME traffic — nights are light)

1. **Newer image** — the pulled `latest` is ~2 years old; a current trunk-recorder has
   better P25 Phase II / simulcast handling. Try a recent tagged image or build.
2. **PPM calibration** — dongles measured large, unconverged ppm; a wrong offset guts
   grant decode even when the strong ID broadcast survives. Calibrate each (against the
   live control channel or a known reference) and set `ppm` per source.
3. **Modulation** — `qpsk` (set) is the simulcast default; test `fsk4` as a fallback.
4. **Gain sweep** — try the R820T steps ~20–40 once ppm is right.
5. **Re-center sources** from `no recorder covering freq` logs so voice channels are covered.

## After decode works

- Add **Rdio Scanner** (self-hosted, phone feed + history) as a second container; point
  trunk-recorder's `rdioscanner_uploader` plugin at it.
- Populate `talkgroups.csv` with Fire/EMS labels from RadioReference (system 556 / CRRS).
- Wire **Jarvis**: read trunk-recorder's call log/metadata to announce/alert on chosen
  talkgroups and relay audio to the `jarvisaudio` Pis as remote stations.
