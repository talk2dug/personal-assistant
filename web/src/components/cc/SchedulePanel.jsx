import Panel, { PanelEmpty, PanelError } from './Panel'

/**
 * What he is committed to today, and what is coming tomorrow.
 *
 * Fed by the agenda rather than the reminders table, so it carries the things that
 * actually occupy a day — work meetings, bills leaving the account, paydays, tasks due,
 * dispute deadlines — instead of only the notes he asked Jarvis to nudge him about.
 *
 * Reminders are deliberately absent. Jack: *"I dont need to see reminders in my schedule,
 * those are to just remind me."* A reminder is a prompt to do something, not a commitment
 * with a place in the day, and showing both made the board read busier than his day was.
 *
 * Two columns rather than one list, because "what is left today" and "what am I walking
 * into tomorrow" are different questions and he asked for both at a glance. On a narrow
 * screen they stack.
 */

const KIND_TAG = {
  bill: 'bill', income: 'in', deadline: 'due', task: 'task', event: '',
}

const money = (n) => `$${Math.abs(Number(n || 0)).toLocaleString(undefined, {
  maximumFractionDigits: 0,
})}`

function localDay(offset) {
  const d = new Date()
  d.setDate(d.getDate() + offset)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** An entry's clock time, when it has one. Feed events carry "09:00 · Teams" in detail. */
function timeOf(entry) {
  const match = /\b([0-2]?\d:[0-5]\d)\b/.exec(entry.detail || '')
  return match ? match[1] : null
}

function Column({ label, entries, emptyText }) {
  return (
    <div className="cc-sched-col">
      <div className="cc-sched-head">
        {label}
        <span className="cc-sched-count">{entries.length || ''}</span>
      </div>
      {entries.length === 0 && <div className="cc-sched-none">{emptyText}</div>}
      {entries.map((entry, i) => {
        const time = timeOf(entry)
        return (
          <div className={`cc-sched k-${entry.kind}`} key={`${entry.kind}-${entry.ref ?? i}`}>
            <span className="cc-sched-time">{time || KIND_TAG[entry.kind] || ''}</span>
            <span className="cc-sched-bar" />
            <span className="cc-sched-text" title={entry.title}>{entry.title}</span>
            {entry.amount ? (
              <span className="cc-sched-amt">
                {entry.kind === 'income' ? '+' : '−'}{money(entry.amount)}
              </span>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}

export default function SchedulePanel({ schedule, error, onOpen }) {
  const days = schedule?.days || []
  const todayIso = schedule?.today || localDay(0)
  const tomorrowIso = localDay(1)

  const forDay = (iso) => days.find((d) => d.date === iso)?.entries || []
  const today = forDay(todayIso)
  const tomorrow = forDay(tomorrowIso)
  const overdue = schedule?.overdue || []

  return (
    <Panel
      title="Schedule"
      meta={schedule ? `${today.length} today · ${tomorrow.length} tomorrow` : null}
      onOpen={onOpen}
      className="cc-flexi"
    >
      {error && <PanelError>schedule unavailable — {error}</PanelError>}
      {!error && !schedule && <PanelEmpty>Loading…</PanelEmpty>}

      {!error && schedule && (
        <div className="cc-scrollbody">
          {/* Anything already missed sits above both columns. It does not get less
              important by being yesterday's. */}
          {overdue.length > 0 && (
            <div className="cc-sched-overdue">
              {overdue.length} overdue — {overdue.slice(0, 2).map((e) => e.title).join(', ')}
              {overdue.length > 2 ? '…' : ''}
            </div>
          )}
          <div className="cc-sched-cols">
            <Column label="Today" entries={today} emptyText="Nothing booked." />
            <Column label="Tomorrow" entries={tomorrow} emptyText="Clear." />
          </div>
          {schedule.unavailable?.length > 0 && (
            <div className="cc-sched-warn">
              couldn’t read: {schedule.unavailable.map((u) => u.source).join(', ')}
            </div>
          )}
        </div>
      )}
    </Panel>
  )
}
