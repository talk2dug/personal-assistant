import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import './agenda.css'

/**
 * What is coming — bills, paydays, tasks, reminders, dispute deadlines and his calendar,
 * on one timeline.
 *
 * Grouped by day rather than laid out as a month grid, deliberately. A month grid gives
 * every day the same space, and his days are not the same: the 15th carries rent, a phone
 * bill and a paycheck, and most days carry nothing. A list weights the page by what is
 * actually happening and works on a phone, which is where he reads it.
 *
 * The running balance is the point of putting money and tasks together. Seeing "rent $925"
 * three days before "payday $4,759" is the difference between a calendar and a plan.
 */

const KIND_LABEL = {
  bill: 'Bill', income: 'In', deadline: 'Deadline',
  task: 'Task', reminder: 'Reminder', event: 'Event',
}

const money = (n) => `$${Math.abs(Number(n || 0)).toLocaleString(undefined, {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
})}`

const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

function dayLabel(iso, todayIso) {
  const d = new Date(`${iso}T12:00:00`)
  const today = new Date(`${todayIso}T12:00:00`)
  const diff = Math.round((d - today) / 86400000)
  const stamp = `${WEEKDAY[d.getDay()]} ${d.getDate()} ${d.toLocaleString(undefined, { month: 'short' })}`
  if (diff === 0) return `${stamp} · today`
  if (diff === 1) return `${stamp} · tomorrow`
  if (diff === -1) return `${stamp} · yesterday`
  if (diff < 0) return `${stamp} · ${Math.abs(diff)}d ago`
  return `${stamp} · in ${diff}d`
}

export default function Agenda() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [horizon, setHorizon] = useState(45)

  const load = useCallback(async () => {
    try {
      setData(await api.agenda({ days: horizon }))
      setError(null)
    } catch (err) {
      setError(err?.message || 'could not load the calendar')
    }
  }, [horizon])

  useEffect(() => { load() }, [load])

  if (error) return <p className="ag-error">{error}</p>
  if (!data) return <p className="ag-dim">Loading…</p>

  const { days, today, counts, money_in: moneyIn, money_out: moneyOut } = data
  const overdue = data.overdue || []

  // Running cash position from today forward. Only money entries move it, and it starts
  // at zero rather than at a balance: this is "what this window does to you", not a
  // projected bank balance, and pretending otherwise would be a number he could act on
  // wrongly.
  let running = 0

  return (
    <div className="ag">
      <div className="ag-top">
        <div className="ag-stat"><span className="v in">{money(moneyIn)}</span><span className="l">coming in</span></div>
        <div className="ag-stat"><span className="v out">{money(moneyOut)}</span><span className="l">going out</span></div>
        <div className="ag-stat">
          <span className={`v ${moneyIn - moneyOut >= 0 ? 'in' : 'out'}`}>
            {moneyIn - moneyOut >= 0 ? '+' : '−'}{money(moneyIn - moneyOut)}
          </span>
          <span className="l">net, next {horizon}d</span>
        </div>
        <div className="ag-stat"><span className="v">{counts.task}</span><span className="l">tasks due</span></div>
        <div className="ag-range">
          {[14, 45, 90].map((d) => (
            <button key={d} type="button" className={horizon === d ? 'is-on' : ''}
                    onClick={() => setHorizon(d)}>{d}d</button>
          ))}
        </div>
      </div>

      {data.unavailable?.length > 0 && (
        <p className="ag-warn">
          Could not read: {data.unavailable.map((u) => u.source).join(', ')} — so this view
          is missing whatever those held.
        </p>
      )}

      {overdue.length > 0 && (
        <div className="ag-overdue">
          <h4>{overdue.length} already past</h4>
          <ul>
            {overdue.map((e, i) => (
              <li key={i}>
                <span className={`ag-tag k-${e.kind}`}>{KIND_LABEL[e.kind]}</span>
                {e.title}
                {e.amount ? <b> {money(e.amount)}</b> : null}
              </li>
            ))}
          </ul>
        </div>
      )}

      <ul className="ag-days">
        {days.map((day) => {
          const isPast = day.date < today
          const isToday = day.date === today
          const dayMoney = day.entries.reduce((sum, e) => {
            if (e.kind === 'income') return sum + (e.amount || 0)
            if (e.kind === 'bill') return sum - (e.amount || 0)
            return sum
          }, 0)
          if (!isPast) running += dayMoney
          return (
            <li key={day.date} className={`${isToday ? 'is-today' : ''} ${isPast ? 'is-past' : ''}`}>
              <div className="ag-day-head">
                <span className="ag-day-label">{dayLabel(day.date, today)}</span>
                {dayMoney !== 0 && (
                  <span className={`ag-day-money ${dayMoney > 0 ? 'in' : 'out'}`}>
                    {dayMoney > 0 ? '+' : '−'}{money(dayMoney)}
                  </span>
                )}
                {!isPast && dayMoney !== 0 && (
                  <span className={`ag-running ${running < 0 ? 'neg' : ''}`}>
                    running {running < 0 ? '−' : '+'}{money(running)}
                  </span>
                )}
              </div>
              <ul className="ag-entries">
                {day.entries.map((e, i) => (
                  <li key={i} className={e.urgent ? 'is-urgent' : ''}>
                    <span className={`ag-tag k-${e.kind}`}>{KIND_LABEL[e.kind] || e.kind}</span>
                    <span className="ag-title">{e.title}</span>
                    {e.detail && <em className="ag-detail">{e.detail}</em>}
                    {e.amount ? (
                      <span className={`ag-amt ${e.kind === 'income' ? 'in' : 'out'}`}>
                        {e.kind === 'income' ? '+' : '−'}{money(e.amount)}
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </li>
          )
        })}
      </ul>

      {days.length === 0 && <p className="ag-dim">Nothing dated in this window.</p>}
    </div>
  )
}
