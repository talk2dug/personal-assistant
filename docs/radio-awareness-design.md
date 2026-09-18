# Radio awareness — what the SDR nodes hear, and how Jarvis acts on it

*Built 2026-09-18. Branch `sdr-sensor-nodes`.*

## What Jack asked for

1. The NOAA weather stream transcribed so Jarvis can tell him about severe weather and
   answer "what are the current conditions".
2. Everything on the Fire/EMS scanner transcribed and filtered for what he'd want to know.
3. Collect the RF devices the sensor node hears, learn what is normal, and flag what isn't —
   in particular a car that keeps coming back ("make sure they aren't canvassing the area").

## The shape

```
jarvishackrf2 (Pi)                    jarvisbox (Windows)                     phone
 rtl_fm -> Icecast /weather.mp3 ─┐   JarvisRadio (assistant/radio_main.py)
 rtl_fm -> pace.py -> /scanner ──┼─> ffmpeg -> faster-whisper base.en ─> radio_transcripts
 eas_watch.py (multimon-ng EAS)  │   local LLM (simrig): conditions ──> weather_conditions
   -> eas_events.jsonl ──────────┼─> SSH tail every 60 s ─────────────> radio_items (eas)
jarvishackrf (Pi)                │   local LLM: scanner triage ───────> radio_items (scanner)
 rtl_433 -> rf.db                │
 rf_baseline.py --json ──────────┴─> SSH every 15 min ────────────────> radio_state + radio_items (rf_*)

JarvisCore scheduler: radio_deliver every 60 s -> radio.deliver_pending -> notify() ──────> phone
JarvisCore/JarvisWeb chat: get_weather_conditions / get_scanner_activity / get_rf_surroundings / list_radio_alerts
Staff feed "radio": radio.briefing() for any employee hired to watch it
Command Center: radio_items rows in the event log
```

## Decisions, and why

**Severe-weather alerts come from the SAME header, not from transcription.** NOAA sends
an AFSK digital header (`ZCZC-WXR-TOR-051087+0045-…`) three times before every warning.
`multimon-ng -a EAS` decodes it deterministically on the Pi in under a second. Transcribing
"the National Weather Service has issued…" with a small model and asking an LLM whether it
is serious would be slower and fallible. Validated with a synthesized SAME burst pushed
through the exact 12 kHz / 32 kbps mp3 chain the stream uses — it decodes cleanly, so the
watcher listens to the Icecast mount and needs no second radio.

**Transcription runs on the Jarvis box, in its own process.** faster-whisper `base.en`
transcribes 60 s of NOAA audio in 2.3 s on this CPU (26x realtime), and the transcript is
good enough that the conditions are read out verbatim ("Temperature 87, relative humidity
58 percent, wind north 12…"). The Pi 4 would run the same model near 1x realtime — too
tight for two streams. It is the `JarvisRadio` Windows service (`deploy/windows_service.py
--variant radio`) for the same reason the vision worker is separate: a stuck ffmpeg or a
slow model must never stall the chat process or the scheduler.

**The worker never notifies.** It fills `radio_items`; JarvisCore's 60-second
`radio_deliver` tick is the only path to the phone, through the same `notify()` funnel as
reminders and staff alerts. Urgent items go one per message immediately; notices batch into
one message per tick; info items are never pushed (Command Center and the tools show
them). Anything that sat undelivered for over a day is marked without sending — a stale
warning pushed as new is worse than none.

**Stable keys make repeats one row.** The SAME header repeats three times; the RF report
re-lists a watched vehicle every poll. `radio_items.key` is UNIQUE and `add_item` is
INSERT-OR-IGNORE. A watched vehicle is re-raised only as it escalates (every three more
visits), so the key carries `escalation // 3`.

**A warning for someone else's county is context, not a push.** `eas_watch.py` marks
`affects_me` from a FIPS list (Richmond city, Henrico, Chesterfield, Hanover — set
`EAS_MY_FIPS` in the unit to change). An urgent warning elsewhere is downgraded to a
notice. Weekly/monthly tests are info.

**Feed health is first-class.** A dead stream and a quiet night look identical from the
tables. The worker heartbeats `radio_state` every 10 s per stream and per poll;
`feed_status()` calls anything older than 3 minutes stale, and every tool and the staff
briefing say so explicitly rather than describing conditions from stale data.

**Vehicles are per tyre sensor, not per car.** From the kerb the antenna usually catches
one of a car's four TPMS sensors per pass (almost every TPMS id in the log is a single hit),
so clustering ids into cars is unreliable. Recurrence of the same id across days is the
signal. rtl_433's Renault/Citroen/Hyundai-VDO labels are decoder families, not the make.

**Classification lives on the node (`rf_baseline.py`), not in Jarvis.** The node has the
full event log; Jarvis stores the JSON report it produces. Classes:

| class | rule |
|---|---|
| own | registered (labelled) device |
| fixture-neighbour | non-vehicle, unregistered, heard on 3+ days |
| unknown-new | non-vehicle, first heard within 14 d (alert if within 24 h) |
| vehicle-passing | one visit |
| vehicle-watch | first heard within 14 d AND 3+ separate visits on 2+ days → "check it out" |
| vehicle-regular | 3+ days over a 7+ day span |
| vehicle-repeat | anything else seen more than once |

Visits are sightings separated by more than 10 minutes. Traffic baseline is vehicle visits
per local hour of day, averaged over the window, versus the last 24 h.

## What the LLM does and doesn't decide

- **Conditions extraction** (every 10 min over the last 12 min of weather transcript):
  JSON with temperature, humidity, wind, pressure + trend, sky, heat index, one-sentence
  summary, hazards mentioned, short forecast. Parsed with the same first-`{…}` rule as the
  mail triage; `None` on anything unexpected — silence is the safe failure.
- **Scanner triage** (per transmission): `important` must be a boolean or the verdict is
  discarded. Severity `urgent` is reserved for immediate danger near `radio_home_area`
  (config; free text naming streets/area, never a full address in a prompt).
- The EAS decision is not an LLM decision at all.

## Operating it

- Streams: `http://192.168.0.161:8000/weather.mp3`, `/scanner.mp3` (LAN and tailnet via the
  jarvis-pi subnet route).
- Pi services: `jarvis-fm-weather`, `jarvis-fm-scanner`, `jarvis-eas-watch` (jarvishackrf2);
  `jarvis-rf-sensor`, `jarvis-rf-sweeper` (jarvishackrf). All `Restart=always`, enabled.
- Jarvis box: `JarvisRadio` service; log at `logs/jarvis-radio.log`.
- Label sensors on the node: `python3 ~/rf-sensor/rf_sensorctl.py pending` / `label`.
  Until labelled, own sensors show as `unknown-new`/`fixture-neighbour`.
- Config knobs (`config.json`): `radio_weather_url`, `radio_scanner_url`, `radio_eas_host`,
  `radio_rf_host`, `radio_home_area`, `radio_conditions_interval_seconds`,
  `radio_eas_poll_seconds`, `radio_rf_poll_seconds`.
- Hire a watcher if narrative judgement is wanted: `hire_employee` with "radio"/"scanner"
  in the description infers the `radio` feed; `alert_policy=on_alert` with a precise
  condition. Not required — the deterministic path above already pushes what matters.

## Known limits

- The scanner channels are Fire/EMS talkaround; the CRRS P25 trunk still does not decode
  on RTL-SDRs (simulcast). Most dispatch traffic is therefore not heard yet.
- NOAA "hazards" from the transcript can lag the SAME header by a cycle; the header is the
  authoritative alert.
- The TPMS antenna is the stock whip; range is a few car lengths. A 17 cm quarter-wave
  would widen the vehicle picture.
