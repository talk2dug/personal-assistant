import { Fragment, useCallback, useEffect, useState } from 'react'
import { api } from '../api'

// Where each section lands. Kept here as text for the header only; the real destinations
// are resolved server-side (core/storage_plan.py DESTINATIONS).
const SECTION_DEST = {
  'Side Hustle': 'jarvisbox D:\\Side Hustle',
  Personal: 'jarvisbox E:\\Personal',
  Projects: 'jarvisbox E:\\Projects',
  Junk: 'quarantined, then deleted after 30 days',
}

function bytes(n) {
  let v = Number(n || 0)
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
  return i === 0 ? `${v} B` : `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}

const groupKey = (g) => `${g.section}|${g.bucket}|${g.source_volume_id}`

function groupState(g) {
  if (g.done === g.units) return { tone: 'ok', text: 'done' }
  if (g.failed) return { tone: 'warn', text: `${g.failed} failed` }
  if (g.approved === g.units) return { tone: 'live', text: 'approved' }
  if (g.excluded === g.units) return { tone: 'off', text: 'excluded' }
  if (g.approved || g.excluded) return { tone: 'live', text: `${g.approved} approved · ${g.excluded} excluded` }
  return { tone: 'off', text: 'waiting for you' }
}

function Units({ group, onChanged }) {
  const [units, setUnits] = useState(null)
  const load = useCallback(() => api.inventoryPlanUnits(group).then((d) => setUnits(d.units)), [group])
  useEffect(() => { load() }, [load])
  const set = async (id, status) => {
    await api.inventoryPlanStatus(status, { ids: [id] })
    await load()
    onChanged()
  }
  if (!units) return <tr><td colSpan={7} className="inv-muted">Loading folders…</td></tr>
  return (
    <tr className="plan-units-row">
      <td colSpan={7}>
        <div className="inv-table-wrap plan-units">
          <table className="inv-table">
            <thead>
              <tr><th>From</th><th>To</th><th className="inv-num">Size</th><th className="inv-num">New</th>
                <th className="inv-num">Already kept</th><th>Status</th><th /></tr>
            </thead>
            <tbody>
              {units.map((u) => (
                <tr key={u.id}>
                  <td className="inv-mono plan-path" title={u.source_path}>{u.source_path || '(drive root)'}</td>
                  <td className="inv-mono plan-path" title={u.dest_path || ''}>{u.dest_path || '—'}</td>
                  <td className="inv-num inv-mono">{bytes(u.bytes)}</td>
                  <td className="inv-num inv-mono">{bytes(u.new_bytes)}</td>
                  <td className="inv-num inv-mono inv-faint">{u.dup_bytes ? bytes(u.dup_bytes) : ''}</td>
                  <td><span className={`inv-state plan-${u.status}`}>{u.status}</span></td>
                  <td className="plan-actions">
                    {u.status !== 'excluded'
                      ? <button type="button" className="inv-link" onClick={() => set(u.id, 'excluded')}>Exclude</button>
                      : <button type="button" className="inv-link" onClick={() => set(u.id, 'proposed')}>Include</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {units.length === 500 && <p className="inv-muted">Showing the 500 largest folders in this group.</p>}
        </div>
      </td>
    </tr>
  )
}

/**
 * The consolidation plan: every folder that moves, grouped by where it lands and where it
 * comes from. Approving only marks a group for the mover -- nothing on any drive changes
 * from this page.
 */
export default function MovePlan() {
  const [plan, setPlan] = useState(null)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => api.inventoryPlan()
    .then((d) => { setPlan(d); setError(null) })
    .catch((e) => setError(e.message || String(e))), [])
  useEffect(() => { load() }, [load])

  const act = async (fn) => {
    setBusy(true)
    try { await fn(); await load() } catch (e) { setError(e.message || String(e)) } finally { setBusy(false) }
  }
  const setGroup = (g, status) => act(() => api.inventoryPlanStatus(status, {
    group: { section: g.section, bucket: g.bucket, source_volume_id: g.source_volume_id },
  }))

  if (!plan) return <p className="inv-muted">{error ? `Couldn't load the plan: ${error}` : 'Loading the plan…'}</p>
  if (!plan.groups.length) {
    return (
      <div className="plan">
        <p className="inv-muted">No plan yet.</p>
        <button type="button" className="plan-btn" disabled={busy} onClick={() => act(api.inventoryPlanRebuild)}>Build the plan</button>
      </div>
    )
  }

  const t = plan.totals
  const sections = [...new Set(plan.groups.map((g) => g.section))]
  const approvedBytes = plan.groups.reduce((a, g) => a + (g.approved === g.units ? g.bytes : 0), 0)

  return (
    <div className="plan">
      <section>
        <h3>The plan</h3>
        <div className="plan-tiles">
          <div className="plan-tile"><b>{bytes(t.copy_bytes)}</b><span>to copy between drives</span></div>
          <div className="plan-tile"><b>{bytes(t.dup_bytes)}</b><span>duplicates not copied again</span></div>
          <div className="plan-tile"><b>{bytes(t.junk_bytes)}</b><span>junk (recycle bins etc.)</span></div>
          <div className="plan-tile"><b>{bytes(approvedBytes)}</b><span>approved so far</span></div>
        </div>
        <p className="inv-muted">
          Side hustle is merged by type under D:\Side Hustle. Personal media and projects move as whole folders to E:.
          Everything removed from a drive goes to a <span className="inv-mono">_to_delete</span> folder for 30 days first.
          Approving here moves nothing: it marks a group for the mover.
          {' '}
          <button type="button" className="inv-link" disabled={busy} onClick={() => act(api.inventoryPlanRebuild)}>
            Rebuild from the latest scan
          </button>
        </p>
      </section>

      {sections.map((sec) => (
        <section key={sec}>
          <h3>{sec} <span className="plan-dest">→ {SECTION_DEST[sec] || ''}</span></h3>
          <div className="inv-table-wrap">
            <table className="inv-table plan-groups">
              <thead>
                <tr><th>{sec === 'Side Hustle' ? 'Type' : 'Kind'}</th><th>From</th><th className="inv-num">Folders</th>
                  <th className="inv-num">Size</th><th className="inv-num">{sec === 'Junk' ? '' : 'Already kept'}</th>
                  <th>Status</th><th /></tr>
              </thead>
              <tbody>
                {plan.groups.filter((g) => g.section === sec).map((g) => {
                  const k = groupKey(g)
                  const st = groupState(g)
                  return (
                    <Fragment key={k}>
                      <tr>
                        <td><b>{g.bucket}</b></td>
                        <td>{g.host} <span className="inv-mono">{g.mount}</span> <span className="inv-faint">{g.label}</span>
                          {g.same_drive ? <span className="inv-tag-mini">same drive</span> : null}</td>
                        <td className="inv-num inv-mono">{g.units.toLocaleString()}</td>
                        <td className="inv-num inv-mono">{bytes(g.bytes)}</td>
                        <td className="inv-num inv-mono inv-faint">{sec !== 'Junk' && g.dup_bytes ? bytes(g.dup_bytes) : ''}</td>
                        <td><span className={`inv-state inv-state-${st.tone}`}>{st.text}</span></td>
                        <td className="plan-actions">
                          <button type="button" className="plan-btn" disabled={busy || g.approved === g.units}
                                  onClick={() => setGroup(g, 'approved')}>Approve</button>
                          <button type="button" className="inv-link" disabled={busy}
                                  onClick={() => setGroup(g, g.excluded === g.units ? 'proposed' : 'excluded')}>
                            {g.excluded === g.units ? 'Include' : 'Exclude'}
                          </button>
                          <button type="button" className="inv-link" onClick={() => setOpen(open === k ? null : k)}>
                            {open === k ? 'Hide' : 'Folders'}
                          </button>
                        </td>
                      </tr>
                      {open === k && <Units group={g} onChanged={load} />}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      ))}

      <section>
        <h3>Space freed per drive</h3>
        <div className="plan-tiles">
          {plan.freed_by_drive.filter((f) => f.freed > 1e9).map((f) => (
            <div key={`${f.host}${f.mount}`} className="plan-tile">
              <b>{bytes(f.freed)}</b><span>{f.host} {f.mount} {f.label}</span>
            </div>
          ))}
        </div>
      </section>

      {plan.needs_decision.length > 0 && (
        <section>
          <h3>Needs a decision</h3>
          <p className="inv-muted">These stay where they are. Tag them on the Drive inventory tab, then rebuild the plan.</p>
          <div className="inv-table-wrap">
            <table className="inv-table">
              <tbody>
                {plan.needs_decision.map((n) => (
                  <tr key={`${n.host}${n.mount}${n.source_path}`}>
                    <td>{n.host} <span className="inv-mono">{n.mount}</span></td>
                    <td className="inv-mono">{n.source_path}</td>
                    <td className="inv-num inv-mono">{bytes(n.bytes)}</td>
                    <td className="inv-faint">{n.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  )
}
