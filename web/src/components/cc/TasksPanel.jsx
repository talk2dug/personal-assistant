import { TINT, dueBucket, dueLabel } from '../../lib/cc'
import Panel, { PanelEmpty } from './Panel'

const PRIORITY_TINT = { high: TINT.warn, normal: TINT.base, low: TINT.idle }

// Ordering for the compact panel: anything late, then anything dated, then high
// priority. The bucket a task is in is recomputed in the viewer's own timezone rather
// than trusting the server's UTC day boundary -- "due tonight" should not read as
// "tomorrow" because the request crossed midnight in London.
const BUCKET_ORDER = { overdue: 0, today: 1, week: 2, later: 3, someday: 4 }

/**
 * What Jack actually has to do, and when it is due.
 *
 * Sits where the mockup had a throughput chart, because a bar chart of tasks closed per
 * hour tells you something about the past and nothing about what to do next. This panel
 * is built for the opposite: the three or four things that want doing, ranked, with the
 * overdue count impossible to miss.
 *
 * Tasks with no due date are counted but not shown here — they are not urgent by
 * definition, and letting eighteen undated items push the two late ones off the panel
 * would defeat the purpose. The drill-down modal shows everything.
 */
export default function TasksPanel({ tasks, onOpen }) {
  const items = tasks?.items || []
  const now = new Date()

  const withBucket = items.map((t) => ({ ...t, bucket: dueBucket(t.due_at, now) }))
  const counts = withBucket.reduce((acc, t) => {
    acc[t.bucket] = (acc[t.bucket] || 0) + 1
    return acc
  }, {})

  const ranked = [...withBucket].sort((a, b) => {
    const bucket = BUCKET_ORDER[a.bucket] - BUCKET_ORDER[b.bucket]
    if (bucket !== 0) return bucket
    if (a.due_at && b.due_at) return a.due_at < b.due_at ? -1 : 1
    const prio = { high: 0, normal: 1, low: 2 }
    return (prio[a.priority] ?? 1) - (prio[b.priority] ?? 1)
  })

  const overdue = counts.overdue || 0
  const today = counts.today || 0

  return (
    <Panel
      title="Tasks"
      meta={items.length ? `${overdue} late · ${today} today` : null}
      metaTint={overdue ? TINT.warn : TINT.dim}
      onOpen={onOpen}
      grow
      className="cc-tasks"
    >
      {!tasks && <PanelEmpty>Loading…</PanelEmpty>}
      {tasks && items.length === 0 && <PanelEmpty>Nothing open. Genuinely.</PanelEmpty>}

      {ranked.slice(0, 5).map((task) => {
        const due = dueLabel(task.due_at, now)
        const late = task.bucket === 'overdue'
        return (
          <div className="cc-task" key={task.id}>
            <span className="cc-pip" style={{ background: PRIORITY_TINT[task.priority] || TINT.base }} />
            <span className="cc-task-text">{task.text}</span>
            {task.status === 'doing' && <span className="cc-task-flag">doing</span>}
            <span className="cc-task-due" style={{ color: late ? TINT.warn : due ? TINT.ok : TINT.idle }}>
              {due || '—'}
            </span>
          </div>
        )
      })}

      {items.length > 0 && (
        <div className="cc-task-footer">
          {(counts.someday || 0) > 0 && <span>{counts.someday} undated</span>}
          <span className="cc-rule" />
          <span>{items.length} open</span>
        </div>
      )}
    </Panel>
  )
}
