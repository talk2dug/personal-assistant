import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import './calendar.css'

/**
 * A calendar that behaves like a calendar — month, week and day, with the usual
 * back/forward/today controls.
 *
 * Fed by /api/agenda, so one grid carries everything with a date on it: work meetings from
 * the subscribed .ics feed, bills leaving the account, paydays, tasks due and credit
 * dispute deadlines.
 *
 * Reminders are excluded. Jack: *"I dont need to see reminders in my schedule, those are
 * to just remind me."* They remain their own thing, delivered rather than displayed.
 *
 * All date arithmetic here is LOCAL. The obvious shortcut — `d.toISOString().slice(0,10)`
 * — converts to UTC first, so for anyone west of Greenwich an evening event silently lands
 * on the next day's cell. That bug is live in the finance month grid; this does not repeat
 * it.
 */

const VIEWS = ['month', 'week', 'day']
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

// The band the hour grid covers. Outside it, entries are pinned to the first/last row
// rather than dropped -- a 5am flight must not vanish because the grid starts at 6.
const DAY_START = 6
const DAY_END = 22

const KIND_LABEL = { bill: 'Bill', income: 'In', deadline: 'Deadline', task: 'Task', event: '' }

function isoOf(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function addDays(d, n) {
  const out = new Date(d)
  out.setDate(out.getDate() + n)
  return out
}

function startOfWeek(d) {
  return addDays(d, -d.getDay())
}

const money = (n) => `$${Math.abs(Number(n || 0)).toLocaleString(undefined, { maximumFractionDigits: 0 })}`

/** "09:00" out of a detail string like "09:00 · Teams", or null for an all-day item. */
function timeOf(entry) {
  const m = /\b([0-2]?\d):([0-5]\d)\b/.exec(entry.detail || '')
  return m ? { hour: Number(m[1]), minute: Number(m[2]), text: `${m[1].padStart(2, '0')}:${m[2]}` } : null
}

function rangeFor(view, cursor) {
  if (view === 'day') return [cursor, cursor]
  if (view === 'week') {
    const first = startOfWeek(cursor)
    return [first, addDays(first, 6)]
  }
  // Month, padded to whole weeks so the leading/trailing cells are populated too.
  const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1)
  const last = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 0)
  return [startOfWeek(first), addDays(startOfWeek(last), 6)]
}

function Entry({ entry, compact }) {
  const time = timeOf(entry)
  return (
    <div className={`cal-entry k-${entry.kind}`} title={`${entry.title}${entry.detail ? ` — ${entry.detail}` : ''}`}>
      {time && <span className="cal-entry-time">{time.text}</span>}
      {!time && !compact && KIND_LABEL[entry.kind] ? (
        <span className="cal-entry-time">{KIND_LABEL[entry.kind]}</span>
      ) : null}
      <span className="cal-entry-title">{entry.title}</span>
      {entry.amount ? (
        <span className="cal-entry-amt">{entry.kind === 'income' ? '+' : '−'}{money(entry.amount)}</span>
      ) : null}
    </div>
  )
}

function MonthView({ from, to, byDate, cursor, today, onPick }) {
  const cells = []
  for (let d = new Date(from); d <= to; d = addDays(d, 1)) cells.push(new Date(d))
  return (
    <div className="cal-month">
      {WEEKDAYS.map((w) => <div className="cal-month-head" key={w}>{w}</div>)}
      {cells.map((d) => {
        const iso = isoOf(d)
        const entries = byDate[iso] || []
        const outside = d.getMonth() !== cursor.getMonth()
        return (
          <div
            key={iso}
            className={`cal-cell ${outside ? 'is-outside' : ''} ${iso === today ? 'is-today' : ''}`}
            onClick={() => onPick(d)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => { if (e.key === 'Enter') onPick(d) }}
          >
            <div className="cal-cell-date">{d.getDate()}</div>
            {entries.slice(0, 4).map((e, i) => <Entry entry={e} key={i} compact />)}
            {entries.length > 4 && <div className="cal-more">+{entries.length - 4} more</div>}
          </div>
        )
      })}
    </div>
  )
}

/** Week and day share the hour grid; a day is just a week of one column. */
function TimeGrid({ days, byDate, today, onPick }) {
  const hours = []
  for (let h = DAY_START; h <= DAY_END; h++) hours.push(h)

  const timed = {}
  const allDay = {}
  for (const d of days) {
    const iso = isoOf(d)
    timed[iso] = []
    allDay[iso] = []
    for (const entry of byDate[iso] || []) {
      const t = timeOf(entry)
      if (t) timed[iso].push({ entry, ...t })
      else allDay[iso].push(entry)
    }
  }

  return (
    <div className="cal-timegrid" style={{ '--cols': days.length }}>
      <div className="cal-tg-corner" />
      {days.map((d) => (
        <div
          className={`cal-tg-head ${isoOf(d) === today ? 'is-today' : ''}`}
          key={`h-${isoOf(d)}`}
          onClick={() => onPick(d)}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => { if (e.key === 'Enter') onPick(d) }}
        >
          <span className="cal-tg-dow">{WEEKDAYS[d.getDay()]}</span>
          <span className="cal-tg-num">{d.getDate()}</span>
        </div>
      ))}

      {/* Everything without a clock time, above the grid — bills, tasks, deadlines. */}
      <div className="cal-tg-allday-label">all day</div>
      {days.map((d) => (
        <div className="cal-tg-allday" key={`a-${isoOf(d)}`}>
          {allDay[isoOf(d)].map((e, i) => <Entry entry={e} key={i} />)}
        </div>
      ))}

      {/* Flat children rather than a row wrapper: a nested row would need `subgrid` to
          keep the columns aligned, and that is a needless compatibility bet here. */}
      {hours.map((h) => [
        <div className="cal-tg-hour" key={`h-${h}`}>
          {h % 12 === 0 ? 12 : h % 12}{h < 12 ? 'a' : 'p'}
        </div>,
        ...days.map((d) => {
          const iso = isoOf(d)
          // Anything before the band lands in the first row, anything after in the last,
          // so nothing is silently outside the grid.
          const here = timed[iso].filter(({ hour }) => (
            h === DAY_START ? hour <= DAY_START
              : h === DAY_END ? hour >= DAY_END
                : hour === h))
          return (
            <div className={`cal-tg-slot ${iso === today ? 'is-today' : ''}`} key={`${iso}-${h}`}>
              {here.map(({ entry }, i) => <Entry entry={entry} key={i} />)}
            </div>
          )
        }),
      ])}
    </div>
  )
}

export default function Calendar() {
  const [view, setView] = useState('week')
  const [cursor, setCursor] = useState(() => new Date())
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  const [from, to] = useMemo(() => rangeFor(view, cursor), [view, cursor])
  const today = isoOf(new Date())

  const load = useCallback(async () => {
    try {
      setData(await api.agenda({ start: isoOf(from), end: isoOf(to), exclude: 'reminder' }))
      setError(null)
    } catch (err) {
      setError(err?.message || 'could not load the calendar')
    }
  }, [from, to])

  useEffect(() => { load() }, [load])

  const byDate = useMemo(() => {
    const map = {}
    for (const day of data?.days || []) map[day.date] = day.entries
    return map
  }, [data])

  const step = (dir) => {
    if (view === 'day') return setCursor((c) => addDays(c, dir))
    if (view === 'week') return setCursor((c) => addDays(c, 7 * dir))
    setCursor((c) => new Date(c.getFullYear(), c.getMonth() + dir, 1))
  }

  const title = view === 'month'
    ? cursor.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
    : view === 'day'
      ? cursor.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })
      : `${from.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} – ${to.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`

  const days = []
  for (let d = new Date(from); d <= to; d = addDays(d, 1)) days.push(new Date(d))

  return (
    <div className="cal">
      <div className="cal-bar">
        <div className="cal-nav">
          <button type="button" onClick={() => step(-1)} aria-label="previous">‹</button>
          <button type="button" onClick={() => setCursor(new Date())}>Today</button>
          <button type="button" onClick={() => step(1)} aria-label="next">›</button>
        </div>
        <div className="cal-title">{title}</div>
        <div className="cal-views">
          {VIEWS.map((v) => (
            <button
              type="button"
              key={v}
              className={view === v ? 'is-on' : ''}
              onClick={() => setView(v)}
            >{v}</button>
          ))}
        </div>
      </div>

      {error && <p className="cal-error">{error}</p>}
      {data?.unavailable?.length > 0 && (
        <p className="cal-warn">
          Couldn’t read: {data.unavailable.map((u) => u.source).join(', ')} — so this view is
          missing whatever those held.
        </p>
      )}
      {!data && !error && <p className="cal-dim">Loading…</p>}

      {data && view === 'month' && (
        <MonthView
          from={from} to={to} byDate={byDate} cursor={cursor} today={today}
          onPick={(d) => { setCursor(d); setView('day') }}
        />
      )}
      {data && view !== 'month' && (
        <TimeGrid
          days={days} byDate={byDate} today={today}
          onPick={(d) => { setCursor(d); setView('day') }}
        />
      )}
    </div>
  )
}
