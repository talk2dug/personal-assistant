import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import './company.css'

/**
 * The company dashboard — the store, on one screen.
 *
 * Four questions, four panels, because they are genuinely different questions: what has
 * been made, how it is performing, what is coming next, and how the team reached those
 * conclusions. The last one is read from the agents' Obsidian journals rather than from a
 * status column, because "why did you pick this" cannot be answered by a summary line.
 *
 * Read-only by design. The rate is changed by talking to Jarvis and blockers are cleared
 * on the Projects board; duplicating either here would create two places to do one thing
 * and a race between them.
 */

function Stat({ label, value, note, tone }) {
  return (
    <div className={`co-stat ${tone ? `t-${tone}` : ''}`}>
      <span className="co-stat-value">{value}</span>
      <span className="co-stat-label">{label}</span>
      {note && <span className="co-stat-note">{note}</span>}
    </div>
  )
}

function Panel({ title, meta, children }) {
  return (
    <section className="co-panel">
      <header>
        <h3>{title}</h3>
        {meta && <span className="co-panel-meta">{meta}</span>}
      </header>
      {children}
    </section>
  )
}

export default function Company() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(null)

  const load = useCallback(async () => {
    try {
      setData(await api.company())
      setError(null)
    } catch (err) {
      setError(err?.message || 'could not load the dashboard')
    }
  }, [])

  useEffect(() => { load() }, [load])

  if (error) return <p className="co-error">{error}</p>
  if (!data) return <p className="co-empty">Loading…</p>

  const { policy, made, pipeline, thinking, blocked, catalog } = data
  const money = (c) => `$${((c || 0) / 100).toFixed(2)}`

  return (
    <div className="co">
      <div className="co-stats">
        <Stat
          label="per day"
          value={policy.paused ? 'PAUSED' : policy.products_per_day}
          note={policy.paused ? 'say "resume the store"' : 'say "make it 3 a day"'}
          tone={policy.paused ? 'warn' : 'ok'}
        />
        <Stat label="live listings" value={made.listings_live} note={`${made.listings_total} total`} />
        <Stat label="in the pipeline" value={pipeline.concepts} note={`${pipeline.trend_leads} trend leads`} />
        <Stat
          label="marketing budget"
          value={policy.marketing_is_free_only ? 'FREE ONLY' : money(policy.marketing_budget_cents)}
          note={policy.marketing_is_free_only ? 'no spend until it earns' : 'approved'}
          tone={policy.marketing_is_free_only ? 'ok' : 'warn'}
        />
        <Stat
          label="waiting on you"
          value={blocked.open}
          note={blocked.open ? 'Projects board' : 'nothing'}
          tone={blocked.open ? 'warn' : 'ok'}
        />
        <Stat label="model catalogue" value={catalog.personas} note={`${catalog.images} shots`} />
      </div>

      {blocked.open > 0 && (
        <Panel title="Stopping the business right now" meta={`${blocked.open} open`}>
          <ul className="co-blockers">
            {blocked.items.map((b) => (
              <li key={b.id} className={`p${b.priority}`}>
                <strong>{b.title}</strong>
                {b.blocks && <span> — blocks {b.blocks}</span>}
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel title="What they have made" meta={`${made.listings_live} live`}>
        {made.recent.length === 0 ? (
          <p className="co-empty">
            Nothing listed yet. Products appear here as the team creates them.
          </p>
        ) : (
          <ul className="co-list">
            {made.recent.map((item) => (
              <li key={item.id}>
                <span className={`co-dot s-${item.status}`} />
                <span className="co-item-title">{item.title}</span>
                <span className="co-item-meta">{item.status}</span>
              </li>
            ))}
          </ul>
        )}
        {/* Absent rather than zeroed: "0 views" on a product nobody has posted reads as
            "nobody engaged" instead of "nothing has been posted". */}
        <p className="co-note">
          <strong>Engagement:</strong> {made.engagement_source}
        </p>
      </Panel>

      <Panel title="What they are thinking for tomorrow" meta={`${pipeline.art_briefs} briefs in progress`}>
        {pipeline.next_up.length === 0 ? (
          <p className="co-empty">
            No concept proposed yet. This fills as the product creator works from the
            trend scout&apos;s findings.
          </p>
        ) : (
          <ul className="co-list">
            {pipeline.next_up.map((c) => (
              <li key={c.id}>
                <span className="co-item-title">{c.name || c.title}</span>
                {c.reasoning && <span className="co-item-why">{c.reasoning}</span>}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="How they are reaching these conclusions" meta="from their Obsidian journals">
        {thinking.unavailable && <p className="co-note">{thinking.unavailable}</p>}
        {!thinking.unavailable && thinking.journals.length === 0 && (
          <p className="co-empty">
            No written reasoning yet. Each agent files a note every run — what it looked
            at, what it ruled out, and why — and reads its own last few back before it
            thinks again.
          </p>
        )}
        {thinking.journals.map((entry) => (
          <div className="co-journal" key={entry.agent}>
            <button
              type="button"
              className="co-journal-head"
              onClick={() => setOpen(open === entry.agent ? null : entry.agent)}
            >
              <span>{entry.agent.replace(/_/g, ' ')}</span>
              <span className="co-journal-toggle">{open === entry.agent ? '−' : '+'}</span>
            </button>
            {open === entry.agent && <pre className="co-journal-body">{entry.notes}</pre>}
          </div>
        ))}

        {thinking.recent_runs.length > 0 && (
          <>
            <h4 className="co-sub">Recent runs</h4>
            <ul className="co-runs">
              {thinking.recent_runs.map((r, i) => (
                <li key={i}>
                  <span className={`co-dot s-${r.status}`} />
                  <span className="co-run-agent">{r.agent.replace(/_/g, ' ')}</span>
                  <span className="co-run-summary">{r.summary}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </Panel>

      {policy.last_change && (
        <p className="co-note">
          <strong>Pace last changed:</strong> {policy.last_change.from} → {policy.last_change.to}
          {policy.last_change.reason ? ` — ${policy.last_change.reason}` : ''}
        </p>
      )}
    </div>
  )
}
