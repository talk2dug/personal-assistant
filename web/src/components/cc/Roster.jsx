import { useAgentStatus } from '../../hooks/useAgentStatus'
import { departmentLabel, groupByDepartment } from '../../lib/agents'
import { TINT } from '../../lib/cc'
import Panel, { PanelEmpty, PanelError } from './Panel'

// The roster's status vocabulary, in the same words the Office page uses, coloured so
// the column can be read down its left edge alone: bright = has the GPU, steel =
// working, amber = blocked waiting for something, dim = done or idle.
const STATE_TINT = {
  on_gpu: TINT.ok,
  working: TINT.base,
  waiting_gpu: TINT.warn,
  just_finished: TINT.dim,
  failed: TINT.warn,
  idle: TINT.idle,
}

const STATE_LABEL = {
  on_gpu: 'on the gpu',
  working: 'working',
  waiting_gpu: 'waiting gpu',
  just_finished: 'just finished',
  failed: 'failed',
  idle: 'idle',
}

const BUSY = new Set(['working', 'on_gpu', 'waiting_gpu'])

export default function Roster({ onOpen }) {
  const { status, error } = useAgentStatus()
  const agents = status?.agents || []
  const live = agents.filter((a) => BUSY.has(a.status)).length
  const groups = groupByDepartment(agents)

  return (
    <Panel
      title="Staff roster"
      meta={agents.length ? `${live} live · ${agents.length} on staff` : null}
      onOpen={onOpen}
      className="cc-roster cc-flexi"
    >
      {error && <PanelError>roster unavailable — {error}</PanelError>}
      {!error && !status && <PanelEmpty>Loading roster…</PanelEmpty>}
      {!error && status && agents.length === 0 && <PanelEmpty>Nobody hired yet.</PanelEmpty>}

      <div className="cc-scrollbody">
        {groups.map(([dept, members]) => (
        <div key={dept} className="cc-roster-group">
          <div className="cc-roster-dept">
            {departmentLabel(dept)}
            <span className="cc-rule" />
            {members.filter((m) => BUSY.has(m.status)).length}/{members.length}
          </div>
          {members.map((agent) => (
            <div key={agent.key} className="cc-roster-row">
              <span className="cc-pip" style={{ background: STATE_TINT[agent.status] || TINT.idle }} />
              <span className="cc-roster-name">{agent.title}</span>
              <span className="cc-roster-state" style={{ color: STATE_TINT[agent.status] || TINT.idle }}>
                {STATE_LABEL[agent.status] || agent.status}
              </span>
            </div>
          ))}
        </div>
        ))}
      </div>
    </Panel>
  )
}
