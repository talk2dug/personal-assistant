import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import './credit.css'

/**
 * The credit pipeline: where he stands, what is in flight, and what is owed to him by when.
 *
 * The goal behind the whole section is a mortgage in a few years, which sets the ordering.
 * A blown dispute deadline comes first because it is the only real leverage in the process
 * and it expires quietly — a bureau that misses its 30 days has to delete the item, but
 * only if somebody notices. Utilisation comes next because it is 30% of the score and the
 * fastest thing he can actually move. Everything else is slower and is shown as such.
 *
 * Two distinctions the page refuses to blur:
 *   what a bureau SAYS vs. what he OWES — paying a collection does not remove the mark
 *   a deadline PROVEN by delivery vs. one estimated from a posting date
 */

const BUREAU = { experian: 'Experian', equifax: 'Equifax', transunion: 'TransUnion', other: 'Other' }

function money(v) {
  if (v == null) return '—'
  return `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function pct(r) {
  return r == null ? '—' : `${(r * 100).toFixed(1)}%`
}

function daysUntil(iso) {
  if (!iso) return null
  return Math.round((new Date(`${iso}T00:00:00`) - new Date().setHours(0, 0, 0, 0)) / 86400000)
}

function Empty({ children }) {
  return <p className="cr-empty">{children}</p>
}

/** A dispute's clock. The whole reason letters go certified. */
function Deadline({ dispute }) {
  const { deadline, overdue_by_days: overdue, answered } = dispute
  if (answered) return <span className="cr-deadline is-done">answered</span>
  if (!deadline) return <span className="cr-deadline">not mailed yet</span>
  if (overdue != null && overdue > 0) {
    return (
      <span className="cr-deadline is-overdue">
        OVERDUE by {overdue}d — they must act
      </span>
    )
  }
  const left = daysUntil(deadline.due_on)
  return (
    <span className={`cr-deadline ${deadline.certain ? '' : 'is-estimated'}`}>
      due {deadline.due_on}{left != null && left >= 0 ? ` · ${left}d left` : ''}
      {!deadline.certain && <em> (estimated — delivery unconfirmed)</em>}
    </span>
  )
}

export default function Credit() {
  const [pic, setPic] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    api.creditPicture().then(setPic).catch((e) => setError(e.message))
  }, [])

  useEffect(() => { load() }, [load])

  async function act(fn) {
    setBusy(true)
    try { await fn(); load() } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (error) return <p className="cc-panel-err">{error}</p>
  if (!pic) return <p className="cc-empty">Loading…</p>

  const score = pic.latest_score
  const previous = pic.scores?.[1]
  const delta = score && previous ? (score.score - previous.score) : null
  const util = pic.utilisation

  return (
    <div className="cr-page">
      {/* Where he stands, and the one number the whole section is aimed at. */}
      <header className="cr-head">
        <div className="cr-score">
          <span className="cr-score-label">Score</span>
          <span className="cr-score-value">{score ? score.score : '—'}</span>
          {score && (
            <span className="cr-score-meta">
              {BUREAU[score.bureau] || score.bureau} · {String(score.recorded_on).slice(0, 10)}
              {delta != null && (
                <b className={delta >= 0 ? 'is-up' : 'is-down'}>
                  {delta >= 0 ? `+${delta}` : delta}
                </b>
              )}
            </span>
          )}
        </div>
        <div className="cr-goal">
          <span className="cr-goal-label">Target for a mortgage</span>
          <span className="cr-goal-value">~760–800</span>
          {score && <span className="cr-goal-gap">{Math.max(0, 760 - score.score)} to go</span>}
        </div>
      </header>

      {!score && (
        <Empty>
          No score recorded yet. Tell Jarvis the next one you see, or upload a report —
          nothing here will estimate one for you.
        </Empty>
      )}

      {/* Leverage first: a blown window expires quietly if nobody looks. */}
      {pic.overdue_disputes.length > 0 && (
        <section className="cr-block cr-urgent">
          <h3>Bureaus that missed their deadline ({pic.overdue_disputes.length})</h3>
          <p className="cr-urgent-note">
            Past 30 days with no response. They are obliged to act on these — this is the
            strongest position you will be in, and it only counts if you chase it.
          </p>
          {pic.overdue_disputes.map((d) => (
            <div className="cr-dispute is-overdue" key={d.id}>
              <span className="cr-dispute-who">{BUREAU[d.bureau]} · {d.creditor_name}</span>
              <span className="cr-dispute-what">{d.item_description}</span>
              <Deadline dispute={d} />
            </div>
          ))}
        </section>
      )}

      <section className="cr-block">
        <h3>
          Utilisation
          <span className="cr-sub">30% of the score, and the fastest thing to move</span>
        </h3>
        {util.overall == null ? (
          <Empty>
            No revolving accounts on file yet — which is not the same as 0% utilisation.
            A thin file is its own problem, and the suggestions below are about fixing it.
          </Empty>
        ) : (
          <>
            <div className="cr-util">
              <span className={`cr-util-figure ${util.overall > 0.3 ? 'is-bad' : util.overall > 0.1 ? 'is-ok' : 'is-good'}`}>
                {pct(util.overall)}
              </span>
              <span className="cr-util-detail">
                {money(util.total_balance)} of {money(util.total_limit)}
              </span>
              <span className="cr-util-cost">
                pay {money(util.to_reach_30)} to reach 30% ·
                {' '}{money(util.to_reach_10)} to reach 10%
              </span>
            </div>
            <ul className="cr-cards">
              {util.cards.map((c) => (
                <li key={c.creditor}>
                  <span className="cr-card-name">{c.creditor}</span>
                  <span className="cr-card-bar">
                    <span style={{ width: `${Math.min(100, c.ratio * 100)}%` }}
                          className={c.ratio > 0.3 ? 'is-bad' : ''} />
                  </span>
                  <span className="cr-card-ratio">{pct(c.ratio)}</span>
                  <span className="cr-card-amt">{money(c.balance)} / {money(c.limit)}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="cr-block">
        <h3>
          Disputes in flight
          <span className="cr-sub">{pic.disputes.length} open</span>
        </h3>
        {pic.disputes.length === 0
          ? <Empty>Nothing disputed yet. Upload a report and the specialist will find what is worth challenging.</Empty>
          : pic.disputes.map((d) => (
            <div className="cr-dispute" key={d.id}>
              <span className="cr-dispute-who">{BUREAU[d.bureau]} · {d.creditor_name}</span>
              <span className="cr-dispute-what">{d.item_description}</span>
              <Deadline dispute={d} />
              <span className="cr-dispute-letters">
                {d.letters_sent} letter{d.letters_sent === 1 ? '' : 's'} sent
                {d.last_mailed_at && ` · last ${String(d.last_mailed_at).slice(0, 10)}`}
              </span>
            </div>
          ))}
      </section>

      <section className="cr-block">
        <h3>
          Marks against him
          <span className="cr-sub">each with the date it ages off on its own</span>
        </h3>
        {pic.derogatories.length === 0
          ? <Empty>No derogatory marks recorded — upload a report to populate this.</Empty>
          : (
            <ul className="cr-marks">
              {pic.derogatories.map((m) => (
                <li key={m.id}>
                  <span className="cr-mark-who">{m.creditor}</span>
                  <span className="cr-mark-kind">{m.derogatory}</span>
                  <span className="cr-mark-amt">{money(m.balance)}</span>
                  <span className="cr-mark-off">
                    {m.falls_off_on ? `falls off ${m.falls_off_on}` : 'fall-off date unknown'}
                  </span>
                </li>
              ))}
            </ul>
          )}
      </section>

      <section className="cr-block">
        <h3>
          Credit worth applying for
          <span className="cr-sub">your call — an application is a hard inquiry</span>
        </h3>
        {pic.recommendations.length === 0
          ? <Empty>Nothing suggested yet. The credit specialist proposes these once it has a report to work from.</Empty>
          : pic.recommendations.map((r) => (
            <div className={`cr-rec is-${r.status}`} key={r.id}>
              <div className="cr-rec-head">
                <span className="cr-rec-name">{r.name}</span>
                <span className="cr-rec-kind">{r.kind.replace('_', ' ')}</span>
                {r.est_approval && (
                  <span className={`cr-rec-odds is-${r.est_approval}`}>{r.est_approval}</span>
                )}
                <span className="cr-rec-status">{r.status}</span>
              </div>
              <p className="cr-rec-why">{r.why}</p>
              <div className="cr-rec-meta">
                {r.reward && <span>{r.reward}</span>}
                {r.annual_fee != null && <span>{r.annual_fee ? `${money(r.annual_fee)}/yr` : 'no annual fee'}</span>}
              </div>
              {r.status === 'suggested' && (
                <div className="cr-rec-actions">
                  <button type="button" className="fin-btn-primary" disabled={busy}
                          onClick={() => act(() => api.recommendationToTask(r.id))}>
                    Add to my tasks
                  </button>
                  <button type="button" className="fin-btn-quiet" disabled={busy}
                          onClick={() => act(() => api.updateRecommendation(r.id, { status: 'dismissed' }))}>
                    Not for me
                  </button>
                </div>
              )}
              {r.task_id && <div className="cr-rec-task">On your task list</div>}
            </div>
          ))}
      </section>
    </div>
  )
}
