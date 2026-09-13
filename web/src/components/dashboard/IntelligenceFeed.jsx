import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'

// Everything actually in flight, one combined poll: background work (research/agents/
// staff/ops plans, from /api/active-work -- see routes/active_work.py) plus what's
// waiting on the owner's own decision (the review queue). This is the Command Center's
// "what's actually happening" feed -- real items, not a decorative ticker.
const POLL_MS = 20000

export default function IntelligenceFeed() {
  const [items, setItems] = useState([])
  const [pendingReview, setPendingReview] = useState(0)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [work, review] = await Promise.all([api.activeWork(), api.reviewItems('pending')])
        if (cancelled) return
        setItems(work.items || [])
        setPendingReview(review.pending ?? 0)
        setError(null)
      } catch (e) {
        if (!cancelled) setError(e.message)
      }
    }
    load()
    const id = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  const running = items.filter((i) => i.status === 'running')
  const queued = items.filter((i) => i.status !== 'running')

  return (
    <section className="dash-panel intel-feed">
      <span className="hud-label">Live Intelligence Feed</span>
      {error && <p className="dash-panel-error">{error}</p>}
      {!error && (
        <ul className="intel-list">
          {pendingReview > 0 && (
            <li className="intel-row intel-warn">
              <Link to="/review">
                {pendingReview} item{pendingReview === 1 ? '' : 's'} awaiting your review
              </Link>
            </li>
          )}
          {running.map((i) => (
            <li key={i.id} className="intel-row intel-live">
              {i.label}
              {i.eta_label && <span className="intel-eta">{i.eta_label}</span>}
            </li>
          ))}
          {queued.map((i) => (
            <li key={i.id} className="intel-row intel-queued">{i.label}</li>
          ))}
          {pendingReview === 0 && items.length === 0 && (
            <li className="intel-row intel-muted">Nothing in flight right now.</li>
          )}
        </ul>
      )}
    </section>
  )
}
