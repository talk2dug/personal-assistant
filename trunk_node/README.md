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

## State as of 2026-09-16 evening (honest)

WORKS: both dongles addressed by serial, control channel **857.0875 MHz locks**,
trunk-recorder decodes the **system identity** (ID 2AA, WACN BEE00, NAC 2A3 — matches
RadioReference). Hardware, drivers, dual-SDR addressing, control-channel freq and system
config are all proven correct.

NOT YET: it decodes the periodic system-identity broadcasts but catches **no per-call
grants** (0 recordings), so no Fire/EMS audio yet. This was chased hard and narrowed:

- Correct channels: the real CRRS Site 003 P25 channels are 856.0875 / 856.5375 /
  857.0875(control) / 857.5375 / 859.5375 (from Jack's RadioReference dump). Sources
  re-centered to cover them (the original 853.0 center was on the *legacy Motorola*
  system by mistake). Control channel confirmed, system identity (ID 2AA / WACN BEE00 /
  NAC 2A3 / site 002-003) decodes cleanly.
- **Ruled out:** traffic (tested in evening, not dead of night), encryption hiding
  (tested with `hideEncrypted:false` -- still zero grants, so it's not just hidden
  police), gain (40/28/36 no change), image version (`edge` Dec-2024 build behaves
  identically to `latest`).
- **Conclusion:** grant (TSBK) decode is failing while the strong repeated identity
  broadcasts survive -- the signature of **P25 Phase II *simulcast* decode quality** at
  the edge of what a single RTL-SDR per channel manages. CRRS Site 003 is explicitly
  "Richmond Simulcast." Frequency/PPM is an unlikely cause given the clean instant ID
  decode, but is the one config lever not yet directly tried.

## Next levers (for a calmer session, not proven)

1. **op25** (boatbod fork) instead of trunk-recorder -- often decodes marginal simulcast
   better; different demod chain.
2. **PPM sweep** per dongle against the live control channel (low odds given clean ID
   decode, but cheap to try).
3. **Better SDR** -- an Airspy Mini/R2 is the known fix for stubborn simulcast where
   RTL-SDRs top out. Jack has SDRs; worth checking if any are Airspy-class.
4. Meanwhile, the **conventional** Fire/EMS channels in `richmond_reference.md`
   (854.0125 Fire T/A, 155.340 / 453.975 Ambulance Authority) and Skywarn 146.88 are
   plain FM -- decodable on the spare dongle with `rtl_fm` today, no trunking needed, as
   an always-works fallback while the trunk decode is sorted.

## After decode works



- Add **Rdio Scanner** (self-hosted, phone feed + history) as a second container; point
  trunk-recorder's `rdioscanner_uploader` plugin at it.
- Populate `talkgroups.csv` with Fire/EMS labels from RadioReference (system 556 / CRRS).
- Wire **Jarvis**: read trunk-recorder's call log/metadata to announce/alert on chosen
  talkgroups and relay audio to the `jarvisaudio` Pis as remote stations.
