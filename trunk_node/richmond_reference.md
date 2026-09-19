# Richmond RF reference (from RadioReference ctid 2948, provided by Jack 2026-09-16)

## The system we follow — Capital Region Radio System (CRRS), P25 Phase II
"Site 003 Richmond Simulcast", System ID 2AA / WACN BEE00 / NAC 2A3. Only five channels:

| Freq (MHz) | Role |
|---|---|
| 856.0875 | voice |
| 856.5375 | voice |
| **857.0875** | **control channel** (confirmed live, -3.4 dB) |
| 857.5375 | voice |
| 859.5375 | voice |

Span 856.0875–859.5375 = **3.45 MHz** — comfortably covered by two RTL-SDRs.
Source centers set to: `1000` @ 856.8 (covers 856.0875/856.5375/857.0875/857.5375),
`1001` @ 858.7 (covers 859.5375). **Police encrypted, Fire/EMS in the clear.**

NOTE: the many **851.1375–853.975** entries are the *legacy* Richmond/Henrico/Chesterfield
**Motorola** system being replaced — a different system. Do NOT center a CRRS source there
(the original config's 853.0 center was this mistake).

## Conventional Fire/EMS — analog, trivially decodable (good fallbacks / fireground)
| Freq (MHz) | Mode | Who |
|---|---|---|
| 854.0125 | FMN 156.7 PL | Richmond City **Fire** Talkaround 1 |
| 854.2375 | FMN 156.7 PL | Richmond Police Talkaround 2 |
| 854.5125 | FMN 156.7 PL | Richmond Talkaround 3 |
| 155.3400 | FMN | Richmond **Ambulance Authority** EMS→Hospital |
| 453.9750 | FMN 67.0 PL | Richmond **Ambulance Authority** alerts (backup to TRS) |
| 155.1300 / 155.4900 | P25e | State Capitol Emergency Mgmt/Interop (encrypted-capable) |

These are simplex/talkaround (intermittent) — main dispatch is on the CRRS trunk. But
854.0125 Fire T/A is a plain-FM channel we can monitor on the spare RTL-SDR with no
trunking at all, as an always-works fallback.

## Weather / ham worth adding (the "other important frequencies")
| Freq (MHz) | Who |
|---|---|
| 146.8800 (W4RAT, 74.4 PL) | Richmond Metro **Skywarn** primary (weather spotting net) |
| 145.4300 (74.4 PL) | Richmond Metro Skywarn secondary |
| 47.4200 (146.2 PL) | American Red Cross ops |
| NOAA WX ~162.475/162.55 | not in this DB; add for weather alerts |

## Other trunked systems nearby (context, not targets)
Radio Communications of Virginia (Connect Plus / Type II / Capacity Plus), Philip Morris,
VCU/MCV, hospitals — all business/DMR, not public safety.
