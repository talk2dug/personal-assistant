import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api'

function toISODate(d) {
  return d.toISOString().slice(0, 10)
}

function buildMonthGrid(year, month) {
  // month is 0-indexed. Returns a flat array of Date|null, padded to full weeks.
  const first = new Date(year, month, 1)
  const last = new Date(year, month + 1, 0)
  const cells = []
  for (let i = 0; i < first.getDay(); i++) cells.push(null)
  for (let day = 1; day <= last.getDate(); day++) cells.push(new Date(year, month, day))
  while (cells.length % 7 !== 0) cells.push(null)
  return cells
}

export default function CalendarGrid() {
  const [cursor, setCursor] = useState(() => {
    const now = new Date()
    return { year: now.getFullYear(), month: now.getMonth() }
  })
  const [occurrences, setOccurrences] = useState([])
  const [reminders, setReminders] = useState([])

  const grid = useMemo(() => buildMonthGrid(cursor.year, cursor.month), [cursor])
  const monthLabel = useMemo(
    () => new Date(cursor.year, cursor.month, 1).toLocaleDateString('en-US', { month: 'long', year: 'numeric' }),
    [cursor],
  )

  useEffect(() => {
    const start = toISODate(new Date(cursor.year, cursor.month, 1))
    const end = toISODate(new Date(cursor.year, cursor.month + 1, 0))
    api.financeCalendar(start, end).then((data) => {
      setOccurrences(data.occurrences)
      setReminders(data.reminders)
    })
  }, [cursor])

  const itemsByDate = useMemo(() => {
    const map = {}
    for (const o of occurrences) {
      ;(map[o.date] ??= []).push({
        text: `${o.direction === 'income' ? '+' : '-'}$${o.amount.toFixed(0)} ${o.description}`,
        kind: o.direction,
      })
    }
    for (const r of reminders) {
      const date = r.due_at.slice(0, 10)
      ;(map[date] ??= []).push({ text: r.text, kind: 'reminder' })
    }
    return map
  }, [occurrences, reminders])

  return (
    <div className="calendar-grid-widget">
      <div className="calendar-nav">
        <button onClick={() => setCursor((c) => shiftMonth(c, -1))}>‹</button>
        <span>{monthLabel}</span>
        <button onClick={() => setCursor((c) => shiftMonth(c, 1))}>›</button>
      </div>
      <div className="calendar-weekdays">
        {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((d) => (
          <div key={d} className="calendar-weekday">
            {d}
          </div>
        ))}
      </div>
      <div className="calendar-cells">
        {grid.map((date, i) => {
          const items = date ? itemsByDate[toISODate(date)] : null
          return (
            <div key={i} className={`calendar-cell ${date ? '' : 'empty'}`}>
              {date && (
                <>
                  <div className="calendar-daynum">{date.getDate()}</div>
                  {items?.map((item, j) => (
                    <div key={j} className={`calendar-item ${item.kind}`}>
                      {item.text}
                    </div>
                  ))}
                </>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function shiftMonth({ year, month }, delta) {
  const d = new Date(year, month + delta, 1)
  return { year: d.getFullYear(), month: d.getMonth() }
}
