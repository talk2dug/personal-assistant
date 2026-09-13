import { useEffect, useState } from 'react'
import { api } from '../../api'
import { fmtBytes } from '../../lib/format'

const POLL_MS = 60000

export default function MediaCard({ onClick }) {
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    function load() {
      api.mediaSummary().then((d) => !cancelled && (setSummary(d), setError(null)))
        .catch((e) => !cancelled && setError(e.message))
    }
    load()
    const id = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return (
    <button type="button" className="dash-card" onClick={onClick}>
      <span className="hud-label">Media</span>
      {error && <p className="dash-card-error">{error}</p>}
      {!error && !summary && <p className="dash-card-loading">Loading…</p>}
      {!error && summary && (
        <>
          <div className="dash-card-figure">{fmtBytes(summary.total_bytes)}</div>
          <div className="dash-card-sub">
            {summary.total_files.toLocaleString()} files · {(summary.by_volume || []).length} volumes
          </div>
        </>
      )}
    </button>
  )
}
