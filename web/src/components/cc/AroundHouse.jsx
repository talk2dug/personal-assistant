import { TINT } from '../../lib/cc'
import Panel, { PanelEmpty, PanelError } from './Panel'

/**
 * Around the house — the jarvishackrf sensor node's glance-value summary: how much
 * traffic the street has seen in the last day, how the transmitters around the house
 * break down, and whether anything is asking to be named or watched.
 *
 * A tile, not a table: the board must never exceed 100vh, so this stays a handful of
 * lines and the full 24-hour device list (and the flag-as-mine controls) live in the
 * modal it opens — same rule as every other panel here.
 */
export default function AroundHouse({ rf, error, onOpen }) {
  const counts = rf?.counts || {}
  const traffic = rf?.traffic || {}
  const devices = rf?.devices || []
  const stale = rf?.feed?.stale

  const watch = counts['vehicle-watch'] || 0
  const toName = devices.filter((d) => d.seen_last_24h && !d.registered).length
  const visits24 = traffic.vehicle_visits_last_24h ?? 0
  const perDay = traffic.vehicle_visits_per_day
  // Passing cars in the last 12h; falls back to the window class count until the Pi
  // baseline has been redeployed + re-polled with the new passing_12h field.
  const passing12 = traffic.passing_12h ?? (counts['vehicle-passing'] || 0)

  const meta = rf?.feed?.available
    ? (stale ? 'sensor stale' : `${devices.filter((d) => d.seen_last_24h).length} in 24h`)
    : null
  const metaTint = stale ? TINT.warn : TINT.dim

  return (
    <Panel title="Around the house" meta={meta} metaTint={metaTint} onOpen={onOpen}>
      {error && <PanelError>sensor node unavailable — {error}</PanelError>}
      {!error && !rf && <PanelEmpty>Reading the street…</PanelEmpty>}
      {!error && rf && !rf.feed?.available && (
        <PanelEmpty>No RF baseline collected yet.</PanelEmpty>
      )}

      {!error && rf && rf.feed?.available && (
        <>
          <div className="cc-figure-row">
            <div className="cc-figure-stack">
              <span className="cc-figure">{visits24}</span>
              <span className="cc-delta" style={{ color: TINT.dim }}>
                vehicle visits · 24h
              </span>
            </div>
            <div className="cc-figure-side">
              <span className="cc-subtle">
                {perDay != null ? `${perDay}/day usual` : 'no baseline'}
              </span>
              <span className="cc-subtle">
                {(counts.own || 0)} mine · {(counts['fixture-neighbour'] || 0)} neighbour
              </span>
            </div>
          </div>

          <div className="cc-positions">
            {watch > 0 && (
              <div className="cc-position">
                <span className="cc-pos-code" style={{ color: TINT.warn }}>WATCH</span>
                <span className="cc-pos-qty">unfamiliar vehicle keeps returning</span>
                <span className="cc-pos-pct" style={{ color: TINT.warn }}>{watch}</span>
              </div>
            )}
            {toName > 0 && (
              <div className="cc-position">
                <span className="cc-pos-code" style={{ color: TINT.warn }}>NAME</span>
                <span className="cc-pos-qty">unregistered, seen today</span>
                <span className="cc-pos-pct" style={{ color: TINT.warn }}>{toName}</span>
              </div>
            )}
            {watch === 0 && toName === 0 && (
              <p className="cc-empty is-inline">
                {(counts['unknown-new'] || 0)} new · {passing12} passing 12h · nothing to flag
              </p>
            )}
          </div>
        </>
      )}
    </Panel>
  )
}
