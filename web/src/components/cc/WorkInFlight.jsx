import { TINT } from '../../lib/cc'
import Panel, { PanelEmpty, PanelError } from './Panel'

/**
 * Everything Jarvis and his staff have running right now — research errands, staff
 * assignments, ops plans, queued work.
 *
 * The progress bars are the one place this board has to be careful. Most of these jobs
 * genuinely cannot report a percentage: an LLM working through an assignment has no
 * meaningful progress metric, only "started" and "finished". So a running item with no
 * ETA gets an indeterminate sweep rather than a made-up 60%, and only items that
 * actually carry step counts (ops plans) draw a real bar. Inventing progress is how a
 * dashboard teaches you to ignore it.
 */
export default function WorkInFlight({ items, error, onOpen }) {
  const list = items || []
  const running = list.filter((i) => i.status === 'running')
  const queued = list.filter((i) => i.status !== 'running')

  // "step 3/7" in an ops plan label is a real fraction — use it when it's there.
  const progressOf = (item) => {
    const match = /step (\d+)\/(\d+)/i.exec(item.label || '')
    if (!match) return null
    const [, done, total] = match
    return Math.min(100, Math.round((Number(done) / Math.max(1, Number(total))) * 100))
  }

  return (
    <Panel
      title="Work in flight"
      meta={list.length ? `${running.length} running · ${queued.length} queued` : null}
      onOpen={onOpen}
      grow
    >
      {error && <PanelError>work feed unavailable — {error}</PanelError>}
      {!error && items === null && <PanelEmpty>Loading…</PanelEmpty>}
      {!error && items && list.length === 0 && <PanelEmpty>Nothing in flight. All quiet.</PanelEmpty>}

      {[...running, ...queued].slice(0, 6).map((item) => {
        const pct = progressOf(item)
        const live = item.status === 'running'
        return (
          <div className="cc-work" key={item.id}>
            <div className="cc-work-head">
              <span className="cc-pip" style={{ background: live ? TINT.ok : TINT.idle }} />
              <span className="cc-work-label">{item.label}</span>
              <span className="cc-work-eta">{item.eta_label || (live ? 'running' : 'queued')}</span>
            </div>
            <div className="cc-meter">
              {pct != null ? (
                <span className="cc-meter-fill" style={{ width: `${pct}%`, background: TINT.base }} />
              ) : live ? (
                <span className="cc-meter-sweep" />
              ) : (
                <span className="cc-meter-fill" style={{ width: '4%', background: TINT.idle }} />
              )}
            </div>
          </div>
        )
      })}
    </Panel>
  )
}
