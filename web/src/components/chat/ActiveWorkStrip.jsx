import { useEffect, useState } from 'react'
import { api } from '../../api'

// Personal dashboard task 17: a persistent status strip in the chat window showing what
// Jarvis is doing in the background right now -- queued research, running agents/staff,
// ops plans -- so the owner doesn't have to ask "what's going on" to find out. Read-only,
// like the Office page: nothing here can be acted on, it just tells the truth about what's
// in flight. Collapsed by default so it doesn't compete with the chat log for space.
//
// A similarly-named ActiveWorkPanel already exists on the Dashboard
// (components/dashboard/ActiveWorkPanel.jsx) -- that one is a bigger "you're doing /
// Jarvis is on it / waiting on you" section for the dashboard landing page, aggregated
// client-side from four existing endpoints, and deliberately includes the review queue
// (what's blocked on a decision). This one is scoped to the chat modal specifically (a
// much smaller surface, always-present rather than one page among many) and pulls from
// /api/active-work, a single server-side snapshot that also covers hired-staff work,
// ops plans, and the async work queue -- none of which the dashboard panel reaches -- plus
// a best-effort ETA. Deliberately named and classed differently (ActiveWorkStrip /
// chat-active-work-*) so its styles can never collide with the dashboard panel's, since
// Vite bundles every page's CSS into one file regardless of which page renders it.
const POLL_MS = 20000

export default function ActiveWorkStrip() {
  const [items, setItems] = useState([])
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const result = await api.activeWork()
        if (!cancelled) setItems(result.items || [])
      } catch {
        // A failed poll just means the strip stays as it was / disappears -- never worth
        // interrupting the chat window with an error over a status readout.
        if (!cancelled) setItems([])
      }
    }
    load()
    const id = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  if (items.length === 0) return null

  const runningCount = items.filter((i) => i.status === 'running').length
  const queuedCount = items.length - runningCount
  const parts = []
  if (runningCount > 0) parts.push(`${runningCount} running`)
  if (queuedCount > 0) parts.push(`${queuedCount} queued`)

  return (
    <div className="chat-active-work">
      <button type="button" className="chat-active-work-summary" onClick={() => setExpanded((v) => !v)}>
        <span className="chat-active-work-dot" />
        <span>{parts.join(', ')}</span>
        <span className="chat-active-work-caret">{expanded ? '▾' : '▸'}</span>
      </button>
      {expanded && (
        <ul className="chat-active-work-list">
          {items.map((item) => (
            <li key={item.id} className={`chat-active-work-item is-${item.status}`}>
              <span className="chat-active-work-label">{item.label}</span>
              <span className="chat-active-work-eta">{item.eta_label || (item.status === 'queued' ? 'queued' : '')}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
