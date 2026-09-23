import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import './day.css'

/**
 * The day planner: today's shape, what he has committed to, and what is slipping.
 *
 * Three columns in the order the day is actually lived, plus a week strip above and
 * tomorrow's brief below:
 *
 *   A. THE DAY'S SHAPE   anchors on a timeline, with a NOW line and the open windows
 *                        between them -- where the chosen work actually fits.
 *   B. THE PLAN          what he chose, then a short ranked list to choose FROM, what's
 *                        blocked, and the habits. Choosing is the step this page exists
 *                        for -- a wall of open tasks gets closed, not scrolled.
 *   C. THE DETAIL RAIL   click any anchor or task to open it here: why it's on the list,
 *                        what it needs, its steps, its history. Empty, it holds the
 *                        quick-capture inbox and the day's own notes.
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

function toMinutes(hhmm) {
  if (!hhmm) return null
  const [h, m] = hhmm.split(':').map(Number)
  return h * 60 + m
}

function minutesUntil(at) {
  const mins = toMinutes(at)
  if (mins == null) return null
  const now = new Date()
  return mins - (now.getHours() * 60 + now.getMinutes())
}

function untilText(mins) {
  if (mins == null) return ''
  if (mins < 0) return 'passed'
  if (mins < 60) return `in ${mins} min`
  return `in ${Math.floor(mins / 60)}h ${mins % 60}m`
}

function fmtGap(mins) {
  if (mins < 60) return `${mins}m open`
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return `${h}h${m ? ` ${m}m` : ''} open`
}

/** The info needed to actually do the task -- tappable, because retyping a number is
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

function WeekStrip({ week, viewedDate, todayDate, onPick }) {
  if (!week.length) return null
  return (
    <div className="day-week hud-panel">
      {week.map((d) => (
        <button
          type="button"
          key={d.date}
          className={`day-week-day ${d.date === viewedDate ? 'is-viewed' : ''} ${d.date === todayDate ? 'is-today' : ''}`}
          onClick={() => onPick(d.date)}
        >
          <div className="day-week-top">
            <span className="day-week-wd">{d.weekday}</span>
            <span className="day-week-num">{d.day_num}</span>
          </div>
          <div className="day-week-bar">
            <div className="day-week-bar-fill" style={{ width: `${Math.min(100, d.anchor_count * 20)}%` }} />
          </div>
          <div className="day-week-meta">
            {d.anchor_count} anchor{d.anchor_count === 1 ? '' : 's'}
            {d.hard_count > 0 && <span className="day-week-hard"> · {d.hard_count} hard</span>}
          </div>
          {d.slipping_count > 0 && <div className="day-week-slip">{d.slipping_count} slipping</div>}
        </button>
      ))}
    </div>
  )
}

function Anchor({ item, onLog, onSelect, selected, busy }) {
  const mins = minutesUntil(item.at_time)
  const state = item.today_state
  return (
    <li className={`day-anchor is-${state || 'open'} ${selected ? 'is-selected' : ''}`}>
      <button type="button" className="day-tick" disabled={busy}
              aria-label={state === 'done' ? 'Undo' : 'Mark done'}
              onClick={() => onLog(item.id, state === 'done' ? null : 'done')}>
        {state === 'done' ? '✓' : state === 'skipped' ? '–' : ''}
      </button>
      <span className="day-anchor-time">{item.at_time}</span>
      <button type="button" className="day-anchor-name" onClick={() => onSelect(item)}>
        {CATEGORY_MARK[item.category] || '•'} {item.name}
        {/* Boolean coercion is load-bearing: `hard` arrives from SQLite as 0 or 1, and
            `0 && <span/>` renders a literal 0 next to the name. */}
        {Boolean(item.hard) && <span className="day-hard" title="Cannot slip"> !</span>}
      </button>
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

function TaskCard({ task, picked, onPick, onUnpick, onSelect, selected, busy }) {
  return (
    <div className={`day-task ${picked ? 'is-picked' : ''} ${selected ? 'is-selected' : ''}`}>
      <div className="day-task-head">
        <button type="button" className="day-task-btn" disabled={busy}
                onClick={() => (picked ? onUnpick(task.id) : onPick(task.id))}>
          {picked ? '−' : '+'}
        </button>
        <button type="button" className="day-task-text" onClick={() => onSelect(task)}>
          {task.text}
        </button>
        {!picked && <span className="day-task-score">{Math.round(task.score)}</span>}
      </div>
      {task.reasons?.length > 0 && (
        <div className="day-task-why">{task.reasons.join(' · ')}</div>
      )}
      <Details details={task.details} />
    </div>
  )
}

/** Timestamped items jotted down in passing -- not yet worth the ceremony of a task. */
function CaptureInbox({ onDate, refreshKey, onSorted }) {
  const [items, setItems] = useState([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.dayCapture(onDate, true).then((r) => setItems(r.capture)).catch(() => {})
  }, [onDate])

  useEffect(() => { load() }, [load, refreshKey])

  const submit = async (e) => {
    e.preventDefault()
    if (!text.trim()) return
    setBusy(true)
    try {
      await api.addCapture(text.trim(), onDate)
      setText('')
      load()
    } finally {
      setBusy(false)
    }
  }

  const sort = async (item) => {
    setBusy(true)
    try {
      const { task_id: taskId } = await api.createPersonalTask({ text: item.text })
      await api.sortCapture(item.id, taskId)
      load()
      onSorted?.()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="day-capture">
      <div className="day-rail-head">
        <span>Captured{items.length > 0 ? ` · ${items.length} not sorted` : ''}</span>
      </div>
      <form className="day-capture-form" onSubmit={submit}>
        <input
          type="text" value={text} placeholder="Jot it down…"
          onChange={(e) => setText(e.target.value)} disabled={busy}
        />
        <button type="submit" disabled={busy || !text.trim()}>Add</button>
      </form>
      <div className="day-capture-list">
        {items.map((item) => (
          <div key={item.id} className="day-capture-item">
            <span className="day-capture-time">{item.captured_at?.slice(11, 16)}</span>
            <span className="day-capture-text">{item.text}</span>
            <button type="button" disabled={busy} onClick={() => sort(item)}>Sort</button>
          </div>
        ))}
        {items.length === 0 && <div className="cc-empty">Nothing captured yet.</div>}
      </div>
    </div>
  )
}

function Notes({ onDate }) {
  const [items, setItems] = useState([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.dayNotes(onDate).then((r) => setItems(r.notes)).catch(() => {})
  }, [onDate])

  useEffect(() => { load() }, [load])

  const submit = async (e) => {
    e.preventDefault()
    if (!text.trim()) return
    setBusy(true)
    try {
      await api.addDayNote(text.trim(), onDate)
      setText('')
      load()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="day-notes">
      <div className="day-rail-head"><span>Notes</span></div>
      <form className="day-capture-form" onSubmit={submit}>
        <input
          type="text" value={text} placeholder="Add a note…"
          onChange={(e) => setText(e.target.value)} disabled={busy}
        />
        <button type="submit" disabled={busy || !text.trim()}>Add</button>
      </form>
      <div className="day-notes-list">
        {items.map((n) => (
          <div key={n.id} className="day-notes-item">
            {n.text}
            <button type="button" className="day-notes-del"
                    onClick={() => api.deleteDayNote(n.id).then(load)}>×</button>
          </div>
        ))}
      </div>
    </div>
  )
}

function StepsAndHistory({ task, busy }) {
  const [steps, setSteps] = useState([])
  const [history, setHistory] = useState([])
  const [text, setText] = useState('')

  const load = useCallback(() => {
    api.taskSteps(task.id).then((r) => setSteps(r.steps)).catch(() => {})
    api.taskHistory(task.id).then((r) => setHistory(r.history)).catch(() => {})
  }, [task.id])

  useEffect(() => { load() }, [load])

  const addStep = async (e) => {
    e.preventDefault()
    if (!text.trim()) return
    await api.addTaskStep(task.id, text.trim())
    setText('')
    load()
  }

  const toggle = async (step) => {
    await api.toggleTaskStep(task.id, step.id, step.done ? false : true)
    load()
  }

  return (
    <>
      <div className="day-rail-head"><span>Steps</span></div>
      <div className="day-steps">
        {steps.map((s) => (
          <div key={s.id} className="day-step">
            <button type="button" className="day-tick" disabled={busy} onClick={() => toggle(s)}>
              {s.done ? '✓' : ''}
            </button>
            <span className={s.done ? 'is-done' : ''}>{s.text}</span>
          </div>
        ))}
        <form className="day-capture-form" onSubmit={addStep}>
          <input type="text" value={text} placeholder="Add a step…"
                 onChange={(e) => setText(e.target.value)} />
          <button type="submit" disabled={!text.trim()}>Add</button>
        </form>
      </div>

      <div className="day-rail-head"><span>History</span></div>
      <div className="day-history">
        {history.map((e) => (
          <div key={e.id} className="day-history-row">
            <span className="day-history-when">{e.at?.slice(0, 16).replace('T', ' ')}</span>
            <span className="day-history-what">{e.kind}{e.detail ? ` · ${e.detail}` : ''}</span>
          </div>
        ))}
        {history.length === 0 && <div className="cc-empty">No history yet.</div>}
      </div>
    </>
  )
}

function DetailRail({ selected, onClose, busy, onLog, picked, onPick, onUnpick, onCaptureSorted, onDate }) {
  if (!selected) {
    return (
      <div className="day-rail-panel hud-panel">
        <div className="day-rail-empty">
          Click any task or anchor to open it here — reasons, what it needs, steps and
          history, and what happened to it so far.
        </div>
        <CaptureInbox onDate={onDate} onSorted={onCaptureSorted} />
        <Notes onDate={onDate} />
      </div>
    )
  }

  if (selected.kind === 'anchor') {
    const item = selected.item
    return (
      <div className="day-rail-panel hud-panel">
        <div className="day-rail-top">
          <span className="day-rail-kind">Anchor</span>
          <button type="button" className="day-rail-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <h2 className="day-rail-title">{CATEGORY_MARK[item.category] || '•'} {item.name}</h2>
        <div className="day-rail-meta-grid">
          <div><span className="day-rail-meta-l">Time</span><span className="day-rail-meta-v">{item.at_time}</span></div>
          <div><span className="day-rail-meta-l">Category</span><span className="day-rail-meta-v">{item.category}</span></div>
          <div><span className="day-rail-meta-l">Cannot slip</span><span className="day-rail-meta-v">{item.hard ? 'yes' : 'no'}</span></div>
          <div><span className="day-rail-meta-l">State</span><span className="day-rail-meta-v">{item.today_state || 'open'}</span></div>
        </div>
        <div className="day-rail-actions">
          <button type="button" disabled={busy} onClick={() => onLog(item.id, item.today_state === 'done' ? null : 'done')}>
            {item.today_state === 'done' ? 'Undo' : 'Mark done'}
          </button>
          {!item.today_state && (
            <button type="button" disabled={busy} onClick={() => onLog(item.id, 'skipped')}>Skip</button>
          )}
        </div>
      </div>
    )
  }

  const task = selected.item
  return (
    <div className="day-rail-panel hud-panel">
      <div className="day-rail-top">
        <span className="day-rail-kind">Task</span>
        <button type="button" className="day-rail-close" onClick={onClose} aria-label="Close">×</button>
      </div>
      <h2 className="day-rail-title">{task.text}</h2>
      {task.reasons?.length > 0 && (
        <>
          <div className="day-rail-head"><span>Why it's on the list</span></div>
          <ul className="day-rail-reasons">
            {task.reasons.map((r, i) => <li key={i}>— {r}</li>)}
          </ul>
        </>
      )}
      {task.details?.length > 0 && (
        <>
          <div className="day-rail-head"><span>What it needs</span></div>
          <Details details={task.details} />
        </>
      )}
      <StepsAndHistory task={task} busy={busy} />
      <div className="day-rail-actions">
        {picked
          ? <button type="button" disabled={busy} onClick={() => onUnpick(task.id)}>Unpick</button>
          : <button type="button" disabled={busy} onClick={() => onPick(task.id)}>Pick for today</button>}
      </div>
    </div>
  )
}

function TomorrowPanel() {
  const [brief, setBrief] = useState(null)
  useEffect(() => { api.tomorrowBrief().then(setBrief).catch(() => {}) }, [])
  if (!brief) return null
  return (
    <div className="day-tomorrow hud-panel">
      <div className="day-tomorrow-head">
        <span>Tomorrow</span>
        <span className="day-tomorrow-date">{brief.weekday}, {brief.date}</span>
        <span className="day-tomorrow-count">
          {brief.anchors.length} anchor{brief.anchors.length === 1 ? '' : 's'}
          {brief.cannot_slip.length > 0 && ` · ${brief.cannot_slip.length} cannot slip`}
          {brief.carries_over.length > 0 && ` · ${brief.carries_over.length} carr${brief.carries_over.length === 1 ? 'ies' : 'y'} over`}
        </span>
      </div>
      <div className="day-tomorrow-cols">
        <div className="day-tomorrow-col">
          <div className="day-tomorrow-col-label">First up</div>
          {brief.anchors.slice(0, 3).map((a) => (
            <div key={a.id} className="day-tomorrow-row">
              <span className="day-tomorrow-time">{a.at_time}</span><span>{a.name}</span>
            </div>
          ))}
          {brief.anchors.length === 0 && <div className="cc-empty">Nothing anchored.</div>}
        </div>
        <div className="day-tomorrow-col is-warn">
          <div className="day-tomorrow-col-label">Cannot slip</div>
          {brief.cannot_slip.map((a) => (
            <div key={a.id} className="day-tomorrow-row"><span>{a.name} — {a.at_time}</span></div>
          ))}
          {brief.cannot_slip.length === 0 && <div className="cc-empty">Nothing that can't move.</div>}
        </div>
        <div className="day-tomorrow-col">
          <div className="day-tomorrow-col-label">Carries over</div>
          {brief.carries_over.map((t) => (
            <div key={t.id} className="day-tomorrow-row">{t.text}</div>
          ))}
          {brief.carries_over.length === 0 && <div className="cc-empty">Nothing carrying over.</div>}
        </div>
        <div className="day-tomorrow-col">
          <div className="day-tomorrow-col-label">Prep tonight</div>
          {brief.prep_tonight.map((p, i) => (
            <div key={i} className="day-tomorrow-row">{p.text}</div>
          ))}
          {brief.prep_tonight.length === 0 && <div className="cc-empty">Nothing to prep.</div>}
        </div>
      </div>
    </div>
  )
}

function weekStart(iso) {
  const d = new Date(`${iso}T00:00:00`)
  const day = (d.getDay() + 6) % 7 // Monday = 0
  d.setDate(d.getDate() - day)
  return d.toISOString().slice(0, 10)
}

export default function Day() {
  const todayDate = useMemo(() => new Date().toISOString().slice(0, 10), [])
  const [viewedDate, setViewedDate] = useState(null) // null means "today" to the API
  const [plan, setPlan] = useState(null)
  const [week, setWeek] = useState([])
  const [selected, setSelected] = useState(null) // { kind: 'anchor'|'task', id }
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const effectiveDate = viewedDate || plan?.date || todayDate

  const load = useCallback(() => {
    api.dayPlan(viewedDate).then(setPlan).catch((e) => setError(e.message))
  }, [viewedDate])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    api.weekShape(weekStart(effectiveDate)).then((r) => setWeek(r.week)).catch(() => {})
  }, [effectiveDate])

  async function act(fn) {
    setBusy(true)
    try {
      await fn()
      load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const logRhythm = (id, state) => act(() =>
    state === null ? api.clearRhythmLog(id, viewedDate) : api.logRhythm(id, state, viewedDate))

  const pick = (id) => act(() => api.pickForDay(id, viewedDate))
  const unpick = (id) => act(() => api.unpickForDay(id, viewedDate))

  const selectAnchor = (item) => setSelected({ kind: 'anchor', id: item.id })
  const selectTask = (item) => setSelected({ kind: 'task', id: item.id })

  if (error) return <p className="cc-panel-err">{error}</p>
  if (!plan) return <p className="cc-empty">Loading…</p>

  // Always resolved fresh from `plan` rather than cached at selection time, so the rail
  // never shows a stale copy of a task/anchor after a refetch changes its fields.
  const resolved = selected && (
    selected.kind === 'anchor'
      ? plan.anchors.find((a) => a.id === selected.id)
      : [...plan.picked, ...plan.pick_from, ...plan.blocked].find((t) => t.id === selected.id)
  )
  const activeSelection = resolved ? { kind: selected.kind, item: resolved } : null

  const nextAnchor = plan.anchors.find((a) => !a.today_state && minutesUntil(a.at_time) >= 0)
  const doneCount = plan.anchors.filter((a) => a.today_state === 'done').length
  const skippedCount = plan.anchors.filter((a) => a.today_state === 'skipped').length
  const leftCount = plan.anchors.length - doneCount - skippedCount
  const isToday = effectiveDate === todayDate

  // Gaps between consecutive anchors -- where chosen work actually fits.
  const timelineRows = []
  const sorted = [...plan.anchors].sort((a, b) => (a.at_time || '').localeCompare(b.at_time || ''))
  const nowMins = new Date().getHours() * 60 + new Date().getMinutes()
  let nowPlaced = false
  for (let i = 0; i < sorted.length; i++) {
    const a = sorted[i]
    const aMins = toMinutes(a.at_time)
    const prevMins = i > 0 ? toMinutes(sorted[i - 1].at_time) : null
    if (isToday && !nowPlaced && aMins != null && nowMins < aMins) {
      timelineRows.push({ type: 'now' })
      nowPlaced = true
    }
    if (prevMins != null && aMins != null) {
      const gap = aMins - prevMins
      if (gap >= 30) timelineRows.push({ type: 'gap', minutes: gap })
    }
    timelineRows.push({ type: 'anchor', item: a })
  }
  if (isToday && !nowPlaced && sorted.length) timelineRows.push({ type: 'now' })

  return (
    <div className="day-page">
      <header className="day-head">
        <h2>{plan.weekday}</h2>
        <span className="day-date">{plan.date}</span>
        {!isToday && (
          <button type="button" className="day-today-btn" onClick={() => setViewedDate(null)}>back to today</button>
        )}
        <div className="day-head-right">
          {nextAnchor && (
            <span className="day-next">
              next: {nextAnchor.name} {untilText(minutesUntil(nextAnchor.at_time))}
            </span>
          )}
          <span className="day-summary">
            {doneCount} done · {skippedCount} skipped · {leftCount} left · {plan.picked.length} chosen of {plan.open_count} open
          </span>
        </div>
      </header>

      <WeekStrip week={week} viewedDate={effectiveDate} todayDate={todayDate}
                 onPick={(d) => setViewedDate(d === todayDate ? null : d)} />

      <div className="day-spread">
        <section className="day-col day-col-shape hud-panel">
          <div className="day-rail-head">
            <span>The day's shape</span>
            <span className="day-count">{plan.anchors.length} anchors</span>
          </div>
          <ul className="day-anchors">
            {timelineRows.map((row, i) => {
              if (row.type === 'now') return <li key={`now-${i}`} className="day-now-line">NOW</li>
              if (row.type === 'gap') return <li key={`gap-${i}`} className="day-gap">{fmtGap(row.minutes)}</li>
              return (
                <Anchor key={row.item.id} item={row.item} onLog={logRhythm}
                        onSelect={selectAnchor}
                        selected={selected?.kind === 'anchor' && selected.id === row.item.id}
                        busy={busy} />
              )
            })}
            {plan.anchors.length === 0 && <li className="cc-empty">Nothing anchored today.</li>}
          </ul>
        </section>

        <section className="day-col day-col-plan hud-panel">
          <div className="day-rail-head">
            <span>Today's plan</span>
            <span className="day-count">{plan.picked.length} chosen · {plan.open_count} open</span>
          </div>

          {plan.picked.length > 0 ? (
            <div className="day-picked">
              {plan.picked.map((t) => (
                <TaskCard key={t.id} task={t} picked busy={busy}
                          onUnpick={unpick} onSelect={selectTask}
                          selected={selected?.kind === 'task' && selected.id === t.id} />
              ))}
            </div>
          ) : (
            <p className="day-nothing">Nothing chosen yet. Pick one or two from below — not five.</p>
          )}

          {plan.pick_from.length > 0 && (
            <>
              <div className="day-sub">Pick from</div>
              <div className="day-options">
                {plan.pick_from.map((t) => (
                  <TaskCard key={t.id} task={t} busy={busy}
                            onPick={pick} onSelect={selectTask}
                            selected={selected?.kind === 'task' && selected.id === t.id} />
                ))}
              </div>
            </>
          )}

          {plan.blocked.length > 0 && (
            <>
              <div className="day-sub">Blocked ({plan.blocked.length}) — not today</div>
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

          <div className="day-rail-head" style={{ marginTop: 14 }}>
            <span>Keeping balance</span>
            {plan.slipping.length > 0 && <span className="day-count is-warn">{plan.slipping.length} slipping</span>}
          </div>
          <ul className="day-habits">
            {plan.habits.map((h) => (
              <li key={h.id} className={`day-habit ${h.slipping ? 'is-slipping' : ''}`}>
                <button type="button" className="day-tick" disabled={busy}
                        aria-label="Log one"
                        onClick={() => logRhythm(h.id, h.today_state === 'done' ? null : 'done')}>
                  {h.today_state === 'done' ? '✓' : ''}
                </button>
                <span className="day-habit-name">{CATEGORY_MARK[h.category] || '•'} {h.name}</span>
                <span className="day-habit-rate">{h.this_week}/{h.target_per_week} this week</span>
                <span className="day-habit-last">
                  {h.days_since == null ? 'never done' : h.days_since === 0 ? 'today' : `${h.days_since}d ago`}
                </span>
              </li>
            ))}
            {plan.habits.length === 0 && <li className="cc-empty">Nothing tracked yet.</li>}
          </ul>
        </section>

        <DetailRail selected={activeSelection} onClose={() => setSelected(null)} busy={busy}
                    onLog={logRhythm} picked={selected?.kind === 'task' && plan.picked.some((t) => t.id === selected.id)}
                    onPick={pick} onUnpick={unpick} onCaptureSorted={load} onDate={effectiveDate} />
      </div>

      <TomorrowPanel />
    </div>
  )
}
