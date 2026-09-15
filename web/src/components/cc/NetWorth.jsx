import { TINT, fmtUsd, sparkPath } from '../../lib/cc'
import Panel, { PanelEmpty, PanelError } from './Panel'

const W = 320
const H = 96

/**
 * Net worth, with its own 90-day trend.
 *
 * The line is drawn from net_worth_snapshots — real recorded history, which means a
 * fresh install has one point and nothing to draw. That case renders the number with a
 * note instead of an empty chart frame, because an axis with no line in it looks like a
 * bug rather than a young dataset.
 */
export default function NetWorth({ finance, history, error, onOpen }) {
  const snapshots = (history || []).slice(-90)
  const values = snapshots.map((s) => s.net_worth ?? s.total ?? s.value).filter((v) => typeof v === 'number')

  const current = values.length ? values[values.length - 1] : finance?.total_balance
  // A week ago, or the oldest point we have if the history is younger than that.
  const prior = values.length > 7 ? values[values.length - 8] : values[0]
  const delta = current != null && prior != null ? current - prior : null
  const up = (delta ?? 0) >= 0

  const line = sparkPath(values, W, H, 8)

  return (
    <Panel
      title="Net worth"
      meta={values.length ? `${values.length}d history` : null}
      onOpen={onOpen}
    >
      {error && <PanelError>finance unavailable — {error}</PanelError>}
      {!error && !finance && !history && <PanelEmpty>Loading…</PanelEmpty>}

      {!error && (finance || values.length > 0) && (
        <>
          <div className="cc-figure-row">
            <span className="cc-figure is-lg">{fmtUsd(current)}</span>
            {delta != null && (
              <span className="cc-delta" style={{ color: up ? TINT.ok : TINT.warn }}>
                {up ? '▲' : '▼'} {fmtUsd(Math.abs(delta))} / 7d
              </span>
            )}
          </div>

          {line ? (
            <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="cc-chart" aria-hidden="true">
              <line x1="0" y1="24" x2={W} y2="24" stroke="rgba(148,188,227,.1)" strokeWidth="1" />
              <line x1="0" y1="48" x2={W} y2="48" stroke="rgba(148,188,227,.1)" strokeWidth="1" />
              <line x1="0" y1="72" x2={W} y2="72" stroke="rgba(148,188,227,.1)" strokeWidth="1" />
              <path d={`${line} L ${W} ${H} L 0 ${H} Z`} fill="rgba(148,188,227,.14)" />
              <path d={line} fill="none" stroke={TINT.ok} strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
            </svg>
          ) : (
            <p className="cc-empty is-inline">
              {values.length === 1 ? 'One snapshot so far — the line starts tomorrow.' : 'No history recorded yet.'}
            </p>
          )}

          <div className="cc-statline">
            <span>{(finance?.accounts || []).length} accounts</span>
            {finance?.safe_to_spend != null && (
              <span style={{ color: TINT.ok }}>{fmtUsd(finance.safe_to_spend)} safe to spend</span>
            )}
            {finance?.liability_balance ? (
              <span>{fmtUsd(Math.abs(finance.liability_balance))} owed</span>
            ) : null}
          </div>
        </>
      )}
    </Panel>
  )
}
