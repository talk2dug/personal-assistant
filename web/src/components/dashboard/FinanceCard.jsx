import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'
import { fmtUsd } from '../../lib/format'

const POLL_MS = 30000

export default function FinanceCard() {
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    function load() {
      api.financeSummary().then((d) => !cancelled && (setSummary(d), setError(null)))
        .catch((e) => !cancelled && setError(e.message))
    }
    load()
    const id = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return (
    <Link to="/finance" className="dash-card">
      <span className="hud-label">Finance</span>
      {error && <p className="dash-card-error">{error}</p>}
      {!error && !summary && <p className="dash-card-loading">Loading…</p>}
      {!error && summary && (
        <>
          <div className="dash-card-figure">{fmtUsd(summary.total_balance)}</div>
          <div className="dash-card-sub">{(summary.accounts || []).length} accounts</div>
        </>
      )}
    </Link>
  )
}
