# sensor_node — RF awareness on jarvishackrf (RTL-SDR decode + HackRF survey)

Deployed to `/home/pi/rf-sensor/` on jarvishackrf (192.168.0.160). Pure stdlib.

| file | role |
|---|---|
| `rf_store.py` | SQLite schema + helpers: `rf_devices` (registry), `rf_events` (log), `rf_signals` (survey) |
| `rf_collector.py` | `jarvis-rf-sensor` service: `rtl_433 -F json` on the RTL-SDR -> registry/log |
| `rf_sweeper.py` | `jarvis-rf-sweeper` service: `hackrf_sweep` 300-450 MHz every 5 min -> `rf_signals` |
| `rf_sensorctl.py` | operator CLI: `pending`, `label <id> "Front Door" --location Entry --type door`, `list`, `recent`, `signals`, `export` |
| `rf_baseline.py` | **what is normal here**: classifies every transmitter (own / neighbour fixture / passing / regular / WATCH vehicle / unknown-new), vehicle traffic by hour vs baseline, and emits alerts with stable keys. `--json` is what Jarvis polls over SSH every 15 min (`assistant/radio_main.py`). |

Vehicles are tyre-pressure sensors (`type: TPMS` in the rtl_433 JSON). One car = four
sensors but the kerbside antenna usually catches one per pass, so recurrence of the same
sensor id across days is the signal, not clustering. A vehicle first heard within 14 days
with 3+ separate visits (10-minute gap) on 2+ days is `vehicle-watch` — "check that car
out". See `docs/radio-awareness-design.md` for the whole picture.

Deploy: `scp sensor_node/*.py pi@192.168.0.160:/home/pi/rf-sensor/` then
`sudo systemctl restart jarvis-rf-sensor jarvis-rf-sweeper` (rf_baseline needs no service).
