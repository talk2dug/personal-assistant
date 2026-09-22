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

function Product({ item, onPulled }) {
  const [pulling, setPulling] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(null)
  const posts = item.posts || []

  const pull = async () => {
    if (!reason.trim()) return
    setBusy(true)
    try {
      await api.retractProduct(item.id, reason.trim())
      setPulling(false)
      setReason('')
      onPulled()
    } catch (err) {
      setFailed(err?.message || 'could not pull it')
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className={`co-product${item.is_live ? ' live' : ''}`}>
      {/* The picture first. He asked to see what art has been made, and a title is not
          that -- he cannot tell whether a design is wrong by reading its name. */}
      {item.art_brief_id ? (
        <img className="co-product-art" src={`/api/company/art/${item.art_brief_id}`} alt="" />
      ) : (
        <div className="co-product-art co-product-noart">no art yet</div>
      )}

      <div className="co-product-body">
        <h4>{item.title}</h4>
        <p className="co-product-meta">
          {item.price != null && <span className="co-price">${Number(item.price).toFixed(2)}</span>}
          <span className={`co-state s-${item.is_live ? 'live' : item.status}`}>
            {item.is_live ? 'on sale' : item.status === 'delisted' ? 'pulled' : 'being made'}
          </span>
        </p>

        {/* The campaign IS these posts. There is no separate campaign object, and an
            empty panel called "campaign" would promise something that does not exist. */}
        {posts.length > 0 && (
          <details className="co-campaign">
            <summary>
              Marketing — {posts.length} post{posts.length === 1 ? '' : 's'} across{' '}
              {new Set(posts.map((p) => p.platform)).size} platforms
            </summary>
            <ul>
              {posts.map((p) => (
                <li key={p.id}>
                  <span className="co-platform">{p.platform}</span>
                  <span className="co-hook">{p.hook || p.caption}</span>
                  <span className="co-post-state">{p.external_id ? 'posted' : p.status}</span>
                </li>
              ))}
            </ul>
          </details>
        )}

        {item.status !== 'delisted' && (pulling ? (
          <div className="co-pull">
            {/* The reason is required, and the field says why rather than just refusing
                an empty one. */}
            <label htmlFor={`why-${item.id}`}>
              Why is this coming down? The team reads this before making anything else.
            </label>
            <input
              id={`why-${item.id}`}
              value={reason}
              autoFocus
              placeholder="e.g. too close to a licensed character"
              onChange={(e) => setReason(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') pull() }}
            />
            <div className="co-pull-actions">
              <button type="button" onClick={pull} disabled={busy || !reason.trim()}>
                {busy ? 'Pulling…' : 'Pull it'}
              </button>
              <button type="button" className="ghost" onClick={() => setPulling(false)}>
                Cancel
              </button>
            </div>
            {failed && <p className="co-error">{failed}</p>}
          </div>
        ) : (
          <button type="button" className="co-pull-open" onClick={() => setPulling(true)}>
            Pull from sale
          </button>
        ))}
      </div>
    </article>
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

  const { policy, made, pipeline, thinking, blocked, catalog, pulled = [] } = data
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

      <Panel
        title="What is for sale"
        meta={`${made.listings_live} live${made.listings_waiting ? `, ${made.listings_waiting} being made` : ''}`}
      >
        {made.recent.length === 0 ? (
          <p className="co-empty">
            Nothing listed yet. Products appear here as the team makes them — you do not
            approve anything, they just go up.
          </p>
        ) : (
          <div className="co-products">
            {made.recent.map((item) => (
              <Product key={item.id} item={item} onPulled={load} />
            ))}
          </div>
        )}
        {/* Absent rather than zeroed: "0 views" on a product nobody has posted reads as
            "nobody engaged" instead of "nothing has been posted". */}
        <p className="co-note">
          <strong>Engagement:</strong> {made.engagement_source}
        </p>
      </Panel>

      {pulled.length > 0 && (
        <Panel title="What you pulled, and why" meta={`${pulled.length} recent`}>
          {/* Shown rather than buried: this is the only instruction the team gets about
              this store, and seeing the list is how he can tell whether saying it
              changed anything. */}
          <ul className="co-list">
            {pulled.map((p) => (
              <li key={p.id}>
                <span className="co-item-title">{p.title}</span>
                <span className="co-item-why">{p.reason}</span>
              </li>
            ))}
          </ul>
          <p className="co-note">
            Every line here is read by the product creator, art director, store manager
            and social director before they make anything new.
          </p>
        </Panel>
      )}

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
