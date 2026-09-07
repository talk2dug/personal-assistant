import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'
import './active-work.css'

// Slow poll on purpose: this panel is a status summary, not an animation (compare
// Agents.jsx, which polls every 4s because it's driving a live scene). 15s matches
// Review.jsx's cadence for the same kind of "quietly stay current" surface.
const POLL_MS = 15000

const AGENT_STATUS_LABEL = { working: 'working', on_gpu: 'on the GPU' }
const ACTIVE_AGENT_STATUSES = new Set(Object.keys(AGENT_STATUS_LABEL))

function SectionHeader({ title, count }) {
  return (
    <div className="active-work-section-header">
      <h4>{title}</h4>
      {count > 0 && <span className="active-work-count">{count}</span>}
    </div>
  )
}

/**
 * "What's actually happening right now", pulled from four places that each already
 * exist for their own page and have never been shown side by side:
 *   - personal_tasks marked 'doing'        (the owner's own in-flight to-dos)
 *   - personal_research still 'requested'  (errands Jarvis is out running)
 *   - agents.status's working/on_gpu agents (Jarvis's own live background activity)
 *   - review_items pending count            (what's blocked on the owner, for contrast)
 *
 * One aggregate object per poll, not four independent ones, for the same reason
 * Agents.jsx fetches one snapshot rather than stitching several: a render built from
 * calls that resolved at different moments can show a state that was never true at any
 * single instant. Loading/error is handled once for the whole panel rather than per
 * section for the same reason.
 */
export default function ActiveWorkPanel() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const [tasks, research, agents, reviews] = await Promise.all([
          api.personalTasks({ status: 'doing' }),
          api.personalResearch(10, 'requested'),
          api.agentStatus(),
          api.reviewItems('pending'),
        ])
        if (cancelled) return
        setData({ tasks, research, agents, reviews })
        setError(null)
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    }

    load()
    const id = setInterval(load, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  if (error) {
    return (
      <section className="active-work-panel">
        <h3>Active work</h3>
        <p className="active-work-error">Couldn’t load active work: {error}</p>
      </section>
    )
  }

  if (!data) {
    return (
      <section className="active-work-panel">
        <h3>Active work</h3>
        <p className="empty-hint">Loading…</p>
      </section>
    )
  }

  const { tasks, research, agents, reviews } = data
  const activeAgents = (agents.agents || []).filter((a) => ACTIVE_AGENT_STATUSES.has(a.status))
  const pendingCount = reviews.pending ?? 0
  const jarvisCount = research.length + activeAgents.length

  return (
    <section className="active-work-panel">
      <h3>Active work</h3>

      <div className="active-work-section">
        <SectionHeader title="You’re doing" count={tasks.length} />
        {tasks.length === 0 ? (
          <p className="empty-hint">
            Nothing marked in-progress — tell Jarvis when you start something (“I’m working
            on X”) and it’ll show up here.
          </p>
        ) : (
          <ul className="active-work-list">
            {tasks.map((t) => (
              <li key={`task-${t.id}`}>
                <span className="active-work-item-text">{t.text}</span>
                {t.project_name && <span className="active-work-meta">{t.project_name}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="active-work-section">
        <SectionHeader title="Jarvis is on it" count={jarvisCount} />
        {jarvisCount === 0 ? (
          <p className="empty-hint">Nothing running in the background right now.</p>
        ) : (
          <ul className="active-work-list">
            {research.map((r) => (
              <li key={`research-${r.id}`}>
                <span className="active-work-item-text">{r.topic}</span>
                <span className="active-work-tag">researching</span>
              </li>
            ))}
            {activeAgents.map((a) => (
              <li key={`agent-${a.key}`}>
                <span className="active-work-item-text">{a.title}</span>
                <span className="active-work-tag">{AGENT_STATUS_LABEL[a.status]}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="active-work-section">
        <SectionHeader title="Waiting on you" count={pendingCount} />
        {pendingCount === 0 ? (
          <p className="empty-hint">Nothing waiting for a decision.</p>
        ) : (
          <Link className="active-work-review-link" to="/review">
            {pendingCount} item{pendingCount === 1 ? '' : 's'} in Review →
          </Link>
        )}
      </div>
    </section>
  )
}
