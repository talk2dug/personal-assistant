import { TINT } from '../../lib/cc'
import Panel, { PanelEmpty, PanelError } from './Panel'

/** Local-day comparison, matching how Schedule.jsx formats these — a reminder due at
 *  11pm belongs to tonight, not to tomorrow in UTC. */
function dayOffset(iso, now) {
  const due = new Date(iso)
  if (Number.isNaN(due.getTime())) return null
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate())
  return Math.round((startOfDay(due) - startOfDay(new Date(now))) / 86400000)
}

function tagFor(offset) {
  if (offset === null) return ''
  if (offset < 0) return 'passed'
  if (offset === 0) return 'today'
  if (offset === 1) return 'tomorrow'
  return `+${offset}d`
}

/**
 * What's on the calendar — the same reminders table the CalDAV sync writes into, so
 * anything on the phone's calendar appears here without a second integration.
 *
 * Shows the next few upcoming items rather than the whole day, and quietly keeps
 * anything that has just passed today (a 9am standup is still worth seeing at 9:05,
 * dimmed, because it tells you where you are in the day).
 */
export default function SchedulePanel({ schedule, error, onOpen, now }) {
  const all = (schedule || [])
    .map((r) => ({ ...r, offset: dayOffset(r.due_at, now), ts: new Date(r.due_at).getTime() }))
    .filter((r) => r.ts && (r.ts > now - 3 * 3600 * 1000))
    .sort((a, b) => a.ts - b.ts)

  const today = all.filter((r) => r.offset === 0).length
  const tomorrow = all.filter((r) => r.offset === 1).length

  return (
    <Panel
      title="Schedule"
      meta={all.length ? `${today} today · ${tomorrow} tomorrow` : null}
      onOpen={onOpen}
      className="cc-flexi"
    >
      {error && <PanelError>calendar unavailable — {error}</PanelError>}
      {!error && schedule === null && <PanelEmpty>Loading…</PanelEmpty>}
      {!error && schedule && all.length === 0 && <PanelEmpty>Nothing scheduled. The day is yours.</PanelEmpty>}

      <div className="cc-scrollbody">
        {all.slice(0, 8).map((item) => {
        const passed = item.ts < now
        return (
          <div className={`cc-sched ${passed ? 'is-past' : ''}`} key={item.id}>
            <span className="cc-sched-time">
              {new Date(item.due_at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}
            </span>
            <span className="cc-sched-bar" />
            <span className="cc-sched-text">{item.text}</span>
            <span className="cc-sched-tag" style={{ color: item.offset === 0 ? TINT.dim : TINT.idle }}>
              {tagFor(item.offset)}
            </span>
          </div>
          )
        })}
      </div>
    </Panel>
  )
}
