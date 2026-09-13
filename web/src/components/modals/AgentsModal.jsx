import { useAgentStatus, AGENT_STATUS_LABEL } from '../../hooks/useAgentStatus'
import { departmentLabel, groupByDepartment } from '../../lib/agents'
import Modal from './Modal'

function timeAgo(iso) {
  if (!iso) return null
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

export default function AgentsModal({ onClose }) {
  const { status, error } = useAgentStatus()
  const agents = status?.agents || []

  return (
    <Modal title="Agents" onClose={onClose} wide>
      {error && <p className="modal-error">{error}</p>}
      {!error && !status && <p className="modal-empty">Loading…</p>}
      {!error && status && agents.length === 0 && <p className="modal-empty">No agents on staff.</p>}
      {!error && groupByDepartment(agents).map(([dept, members]) => (
        <div key={dept} className="modal-section">
          <div className="modal-section-title">{departmentLabel(dept)}</div>
          <ul className="modal-row-list">
            {members.map((a) => (
              <li key={a.key} className="modal-row">
                <span className="modal-row-label">
                  {a.title}{a.hired && ` · ${a.seniority}`}
                  {a.detail && <span className="dash-card-sub"> — {a.detail}</span>}
                </span>
                <span className="modal-row-value">
                  {AGENT_STATUS_LABEL[a.status] || a.status}
                  {a.last_run_at && ` · ${timeAgo(a.last_run_at)}`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </Modal>
  )
}
