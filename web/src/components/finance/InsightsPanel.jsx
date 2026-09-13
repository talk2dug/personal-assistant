import { useEffect, useState } from 'react'
import { api } from '../../api'

// Era's forecast_spending/get_cash_flow/compare_spending_periods tools have been
// chat-reachable for a while (engine.py's ERA_CATEGORY_KEYWORDS routes to them already)
// but were never shown anywhere on the dashboard -- this surfaces the same cached
// payloads scheduler.refresh_era_cache now keeps warm (see db.upsert_era_insight).
//
// Their exact response shapes were never confirmed via live testing the way
// insights__analyze_spending's was (see scheduler.py's _era_payload docstring), so rather
// than guess at field names and risk silently dropping real data, this renders whatever
// keys actually come back. The one thing every one of these tools' own descriptions do
// promise is a consistent empty_state {kind, reason, next_step} envelope when a section
// has nothing to show, and an optional top-level warnings array -- both are handled
// explicitly since they're documented, stable conventions rather than a guess.
const INSIGHT_LABELS = {
  forecast_spending: 'Spending forecast',
  cash_flow: 'Cash flow trend',
  compare_spending_periods: 'This month vs. last month',
}

function formatValue(value) {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') return value.toLocaleString('en-US', { maximumFractionDigits: 2 })
  if (Array.isArray(value)) {
    if (value.length === 0) return '—'
    return value
      .map((item) => (item && typeof item === 'object'
        ? Object.entries(item).map(([k, v]) => `${k}: ${v}`).join(', ')
        : String(item)))
      .join(' · ')
  }
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function InsightBody({ payload }) {
  if (!payload) {
    return <p className="empty-hint">Not cached yet — the sync job runs every ~20 min.</p>
  }
  if (payload.empty_state) {
    const { reason, next_step: nextStep } = payload.empty_state
    return (
      <p className="empty-hint">
        {reason || 'No data yet.'}
        {nextStep ? ` — ${nextStep}` : ''}
      </p>
    )
  }

  const fields = Object.entries(payload).filter(([key]) => key !== 'warnings')

  return (
    <>
      {Array.isArray(payload.warnings) && payload.warnings.length > 0 && (
        <ul className="insight-warnings">
          {payload.warnings.map((w, i) => (
            <li key={i}>{typeof w === 'string' ? w : JSON.stringify(w)}</li>
          ))}
        </ul>
      )}
      <dl className="insight-fields">
        {fields.map(([key, value]) => (
          <div className="insight-field" key={key}>
            <dt>{key.replace(/_/g, ' ')}</dt>
            <dd>{formatValue(value)}</dd>
          </div>
        ))}
      </dl>
    </>
  )
}

export default function InsightsPanel() {
  const [insights, setInsights] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.financeInsights().then(setInsights).catch((e) => setError(e.message))
  }, [])

  if (error) return <p className="modal-error">{error}</p>
  if (!insights) return <p className="empty-hint">Loading…</p>

  return (
    <div className="insights-panel">
      {Object.keys(INSIGHT_LABELS).map((key) => (
        <div className="insight-card" key={key}>
          <div className="insight-card-title">{INSIGHT_LABELS[key]}</div>
          <InsightBody payload={insights[key]?.payload} />
        </div>
      ))}
    </div>
  )
}
