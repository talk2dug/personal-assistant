import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import './day.css'

/**
 * The day planner: today's shape, what he has committed to, and what is slipping.
 *
 * Three sections in the order the day is actually lived, and nothing else on screen:
 *
 *   TODAY'S SHAPE   the anchors, with the next one marked. Tickable, because an
 *                   untracked routine cannot tell him what he is drifting on.
 *   TODAY'S PLAN    what he chose, then a short list to choose FROM. Choosing is the
 *                   step this page exists for -- thirty-three open tasks is a wall, and
 *                   a wall gets closed.
 *   BALANCE         the habits, worst first. The reason to spend a slot on something
 *                   with no deadline.
 *
 * Personal track only. Build work has its own home; ranking "wake-word arbitration"
 * against "find a vet for Ghost" produces a list useless for planning either kind of day.
 */

const CATEGORY_MARK = {
  wake: '☀', work: '💼', care: '🐾', health: '🚴', relationship: '❤',
  project: '🔧', fun: '🎮', wind_down: '🌙', other: '•',
}

const DETAIL_ACTION = {
  phone: (v) => `tel:${v.replace(/[^\d+]/g, '')}`,
  address: (v) => `https://maps.apple.com/?q=${encodeURIComponent(v)}`,
  link: (v) => (/^https?:\/\//.test(v) ? v : `https://${v}`),
}

function minutesUntil(at) {
  if (!at) return null
  const [h, m] = at.split(':').map(Number)
  const now = new Date()
  return (h * 60 + m) - (now.getHours() * 60 + now.getMinutes())
}

function untilText(mins) {
  if (mins == null) return ''
  if (mins < 0) return 'passed'
  if (mins < 60) return `in ${mins} min`
  return `in ${Math.floor(mins / 60)}h ${mins % 60}m`
}

/** The info needed to actually do the task — tappable, because retyping a number is
 *  exactly the friction that stops a task getting done. */
function Details({ details }) {
  if (!details?.length) return null
  return (
    <ul className="day-details">
      {details.map((d) => {
        const href = DETAIL_ACTION[d.kind]?.(d.value)
        return (
          <li key={d.id} className={`day-detail is-${d.kind}`}>
            {d.label && <span className="day-detail-label">{d.label}</span>}
            {href
              ? <a href={href} target="_blank" rel="noreferrer">{d.value}</a>
              : <span>{d.value}</span>}
          </li>
        )
      })}
    </ul>
  )
}

function Anchor({ item, onLog, busy }) {
  const mins = minutesUntil(item.at_time)
  const state = item.today_state
  return (
    <li className={`day-anchor is-${state || 'open'}`}>
      <button type="button" className="day-tick" disabled={busy}
              aria-label={state === 'done' ? 'Undo' : 'Mark done'}
              onClick={() => onLog(item.id, state === 'done' ? null : 'done')}>
        {state === 'done' ? '✓' : state === 'skipped' ? '–' : ''}
      </button>
      <span className="day-anchor-time">{item.at_time}</span>
      <span className="day-anchor-name">
        {CATEGORY_MARK[item.category] || '•'} {item.name}
        {/* Boolean coercion is load-bearing: `hard` arrives from SQLite as 0 or 1, and
            `0 && <span/>` renders a literal 0 next to the name. */}
        {Boolean(item.hard) && <span className="day-hard" title="Cannot slip"> !</span>}
      </span>
      <span className="day-anchor-when">
        {state ? state : untilText(mins)}
      </span>
      {!state && (
        <button type="button" className="day-skip" disabled={busy}
                onClick={() => onLog(item.id, 'skipped')}>skip</button>
      )}
    </li>
  )
}

function TaskCard({ task, picked, onPick, onUnpick, busy }) {
  return (
    <div className={`day-task ${picked ? 'is-picked' : ''}`}>
      <div className="day-task-head">
        <button type="button" className="day-task-btn" disabled={busy}
                onClick={() => (picked ? onUnpick(task.id) : onPick(task.id))}>
          {picked ? '−' : '+'}
        </button>
        <span className="day-task-text">{task.text}</span>
        {!picked && <span className="day-task-score">{Math.round(task.score)}</span>}
      </div>
      {task.reasons?.length > 0 && (
        <div className="day-task-why">{task.reasons.join(' · ')}</div>
      )}
      <Details details={task.details} />
    </div>
  )
}

export default function Day() {
  const [plan, setPlan] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    api.dayPlan().then(setPlan).catch((e) => setError(e.message))
  }, [])

  useEffect(() => { load() }, [load])

  async function act(fn) {
    setBusy(true)
    try { await fn(); load() } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const logRhythm = (id, state) => act(() =>
    state === null ? api.clearRhythmLog(id) : api.logRhythm(id, state))

  if (error) return <p className="cc-panel-err">{error}</p>
  if (!plan) return <p className="cc-empty">Loading…</p>

  const nextAnchor = plan.anchors.find((a) => !a.today_state && minutesUntil(a.at_time) >= 0)

  return (
    <div className="day-page">
      <header className="day-head">
        <h2>{plan.weekday}</h2>
        <span className="day-date">{plan.date}</span>
        {nextAnchor && (
          <span className="day-next">
            next: {nextAnchor.name} {untilText(minutesUntil(nextAnchor.at_time))}
          </span>
        )}
      </header>

      <section className="day-block">
        <h3>Today's shape</h3>
        <ul className="day-anchors">
          {plan.anchors.map((a) => (
            <Anchor key={a.id} item={a} onLog={logRhythm} busy={busy} />
          ))}
          {plan.anchors.length === 0 && (
            <li className="cc-empty">Nothing anchored today.</li>
          )}
        </ul>
      </section>

      <section className="day-block">
        <h3>
          Today's plan
          <span className="day-count">
            {plan.picked.length} chosen · {plan.open_count} open
          </span>
        </h3>

        {plan.picked.length > 0 ? (
          <div className="day-picked">
            {plan.picked.map((t) => (
              <TaskCard key={t.id} task={t} picked busy={busy}
                        onUnpick={(id) => act(() => api.unpickForDay(id))} />
            ))}
          </div>
        ) : (
          <p className="day-nothing">
            Nothing chosen yet. Pick one or two from below — not five.
          </p>
        )}

        {plan.pick_from.length > 0 && (
          <>
            <div className="day-sub">Pick from</div>
            <div className="day-options">
              {plan.pick_from.map((t) => (
                <TaskCard key={t.id} task={t} busy={busy}
                          onPick={(id) => act(() => api.pickForDay(id))} />
              ))}
            </div>
          </>
        )}

        {plan.blocked.length > 0 && (
          <>
            <div className="day-sub">
              Blocked ({plan.blocked.length}) — not today
            </div>
            <ul className="day-blocked">
              {plan.blocked.map((t) => (
                <li key={t.id}>
                  <span className="day-blocked-text">{t.text}</span>
                  <span className="day-blocked-why">
                    waiting on: {t.waiting_on.map((b) => b.text).join('; ')}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="day-block">
        <h3>
          Keeping balance
          {plan.slipping.length > 0 && (
            <span className="day-count is-warn">{plan.slipping.length} slipping</span>
          )}
        </h3>
        <ul className="day-habits">
          {plan.habits.map((h) => (
            <li key={h.id} className={`day-habit ${h.slipping ? 'is-slipping' : ''}`}>
              <button type="button" className="day-tick" disabled={busy}
                      aria-label="Log one"
                      onClick={() => logRhythm(h.id, h.today_state === 'done' ? null : 'done')}>
                {h.today_state === 'done' ? '✓' : ''}
              </button>
              <span className="day-habit-name">
                {CATEGORY_MARK[h.category] || '•'} {h.name}
              </span>
              <span className="day-habit-rate">
                {h.this_week}/{h.target_per_week} this week
              </span>
              <span className="day-habit-last">
                {h.days_since == null ? 'never done'
                  : h.days_since === 0 ? 'today'
                  : `${h.days_since}d ago`}
              </span>
            </li>
          ))}
          {plan.habits.length === 0 && <li className="cc-empty">Nothing tracked yet.</li>}
        </ul>
      </section>
    </div>
  )
}
