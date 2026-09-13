import { useEffect, useState } from 'react'
import { api } from '../../api'
import { fmtUsd } from '../../lib/format'

const POLL_MS = 30000

export default function FinanceCard({ onClick }) {
  const [summary, setSummary] = useState(null)
  const [safeToSpend, setSafeToSpend] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    function load() {
      Promise.all([api.financeSummary(), api.financeSafeToSpend()])
        .then(([s, sts]) => !cancelled && (setSummary(s), setSafeToSpend(sts), setError(null)))
        .catch((e) => !cancelled && setError(e.message))
    }
    load()
    const id = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return (
    <button type="button" className="dash-card" onClick={onClick}>
      <span className="hud-label">Finance</span>
      {error && <p className="dash-card-error">{error}</p>}
      {!error && !summary && <p className="dash-card-loading">Loading…</p>}
      {!error && summary && (
        <>
          <div className="dash-card-figure">{fmtUsd(summary.total_balance)}</div>
          <div className="dash-card-sub">{(summary.accounts || []).length} accounts</div>
          {safeToSpend?.safe_to_spend != null && (
            <div className="dash-card-sub dash-card-highlight">
              {fmtUsd(safeToSpend.safe_to_spend)} safe to spend
            </div>
          )}
        </>
      )}
    </button>
  )
}
