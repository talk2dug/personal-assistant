import { Fragment, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import './rf.css'

// The node is polled into jarvis.db every ~15 min, so this just needs to feel current
// while the modal is open; the REFRESH button is the escape hatch for "right now".
const POLL_MS = 20000

const CLASS_META = {
  own: { label: 'Mine', tone: 'mine' },
  'fixture-neighbour': { label: 'Neighbour fixture', tone: 'neighbour' },
  'unknown-new': { label: 'New / unknown', tone: 'new' },
  unknown: { label: 'Unknown', tone: 'idle' },
  'vehicle-passing': { label: 'Vehicle · passing', tone: 'vehicle' },
  'vehicle-repeat': { label: 'Vehicle · repeat', tone: 'vehicle' },
  'vehicle-regular': { label: 'Vehicle · regular', tone: 'vehicle' },
  'vehicle-watch': { label: 'Vehicle · WATCH', tone: 'watch' },
}

// Sort key for the tables: the things that want attention first, cars next, own last.
const CLASS_ORDER = [
  'vehicle-watch', 'unknown-new', 'vehicle-regular', 'vehicle-repeat',
  'fixture-neighbour', 'unknown', 'vehicle-passing', 'own',
]

function classMeta(cls) {
  return CLASS_META[cls] || { label: cls || 'unknown', tone: 'idle' }
}

function timeAgo(iso) {
  if (!iso) return '—'
  const norm = iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`
  const s = Math.max(0, Math.floor((Date.now() - new Date(norm).getTime()) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function hourLabel(h) {
  if (h == null) return ''
  const hr = ((h % 12) || 12)
  return `${hr}${h < 12 ? 'am' : 'pm'}`
}

function fmtMinutes(m) {
  if (m == null) return '—'
  if (m >= 90) return `${(m / 60).toFixed(1)}h`
  return `${Math.round(m)}m`
}

/** How the interval column reads for a device seen today. Vehicles are the case Jack
 *  asked about — "back every ~40 min" vs "once this morning". */
function intervalText(d) {
  const l = d.last_24h
  if (!l) return d.visits_last_24h ? `${d.visits_last_24h}× today` : '—'
  const iv = l.interval_minutes || []
  if (l.visits <= 1) return 'single visit'
  if (iv.length === 0) return `${l.visits}× today`
  return `~${fmtMinutes(l.mean_interval_minutes)} apart`
}

function visits24(d) {
  return d.last_24h?.visits ?? d.visits_last_24h ?? 0
}

function last24Seen(d) {
  return d.last_24h?.last_seen || d.last_seen
}

function FeedPill({ feed }) {
  if (!feed) return null
  const stale = feed.stale || !feed.available
  const age = feed.poll_age_s
  const detail = feed.available
    ? `${age != null ? `${Math.round(age / 60)}m since poll` : 'polled'} · ${feed.host || 'node'}`
    : 'no report yet'
  return (
    <div className={`rf-feed-pill ${stale ? 'rf-feed-stale' : 'rf-feed-live'}`}>
      <span className="rf-feed-dot" />
      {stale ? 'SENSOR STALE' : 'SENSOR LIVE'}
      <span className="rf-feed-detail">{detail}</span>
    </div>
  )
}

function Stat({ label, value, sub, tone }) {
  return (
    <div className={`rf-stat ${tone ? `rf-tone-${tone}` : ''}`}>
      <span className="rf-stat-label">{label}</span>
      <span className="rf-stat-value">{value}</span>
      {sub && <span className="rf-stat-sub">{sub}</span>}
    </div>
  )
}

function ClassBadge({ cls }) {
  const m = classMeta(cls)
  return <span className={`rf-badge rf-tone-${m.tone}`}>{m.label}</span>
}

/** The inline "flag as mine" form that drops under an unregistered row. */
function NameForm({ device, types, busy, onSave, onCancel }) {
  const [name, setName] = useState('')
  const [location, setLocation] = useState('')
  const [type, setType] = useState('motion')
  const [err, setErr] = useState(null)

  const submit = (e) => {
    e.preventDefault()
    if (!name.trim()) { setErr('A name is required.'); return }
    setErr(null)
    onSave({ fingerprint: device.fingerprint, name: name.trim(), location: location.trim(), type })
      .catch((ex) => setErr(ex.message || 'Could not save.'))
  }

  return (
    <form className="rf-name-form" onSubmit={submit}>
      <div className="rf-name-fields">
        <label>
          <span>Name</span>
          <input
            autoFocus
            value={name}
            maxLength={64}
            placeholder="e.g. Front Door motion"
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label>
          <span>Location</span>
          <input
            value={location}
            maxLength={64}
            placeholder="e.g. Entry"
            onChange={(e) => setLocation(e.target.value)}
          />
        </label>
        <label>
          <span>Type</span>
          <select value={type} onChange={(e) => setType(e.target.value)}>
            {types.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
      </div>
      <div className="rf-name-actions">
        <span className="rf-name-fp">{device.fingerprint}</span>
        <div className="rf-name-buttons">
          <button type="button" className="cc-btn" onClick={onCancel} disabled={busy}>CANCEL</button>
          <button type="submit" className="cc-btn is-primary" disabled={busy}>
            {busy ? 'SAVING…' : 'FLAG AS MINE'}
          </button>
        </div>
      </div>
      {err && <p className="rf-name-err">{err}</p>}
    </form>
  )
}

/** The primary table: everything heard in the last 24 hours, with the naming control on
 *  every unregistered row and a distinct look for the ones already flagged as Jack's. */
function DeviceTable({ devices, types, naming, setNaming, onSave, busyFp }) {
  if (devices.length === 0) {
    return <p className="rf-muted">Nothing has been heard in the last 24 hours.</p>
  }
  return (
    <table className="rf-table">
      <thead>
        <tr>
          <th>Device</th><th>Class</th><th className="rf-num">24h</th>
          <th>Interval</th><th>Last seen</th><th className="rf-act">Flag &amp; name</th>
        </tr>
      </thead>
      <tbody>
        {devices.map((d) => {
          const mine = d.registered
          const open = naming === d.fingerprint
          return (
            <Fragment key={d.fingerprint}>
              <tr className={mine ? 'rf-row-mine' : ''}>
                <td>
                  <div className="rf-dev-name">
                    {mine && <span className="rf-mine-tick">◆</span>}
                    {d.label || d.model}
                  </div>
                  <div className="rf-dev-fp">
                    {d.model}{d.location ? ` · ${d.location}` : ''}
                    {mine && d.device_type ? ` · ${d.device_type}` : ''}
                  </div>
                </td>
                <td><ClassBadge cls={d.class} /></td>
                <td className="rf-num">{visits24(d)}</td>
                <td className="rf-interval" title={(d.last_24h?.interval_minutes || []).map((m) => `${m}m`).join(', ')}>
                  {d.vehicle ? intervalText(d) : (d.seen_cmds?.length ? d.seen_cmds.join(', ') : '—')}
                </td>
                <td className="rf-ago">{timeAgo(last24Seen(d))}</td>
                <td className="rf-act">
                  {mine ? (
                    <span className="rf-mine-tag">MINE</span>
                  ) : (
                    <button
                      type="button"
                      className="cc-chip"
                      onClick={() => setNaming(open ? null : d.fingerprint)}
                    >
                      {open ? 'close' : 'name…'}
                    </button>
                  )}
                </td>
              </tr>
              {open && !mine && (
                <tr className="rf-form-row">
                  <td colSpan={6}>
                    <NameForm
                      device={d}
                      types={types}
                      busy={busyFp === d.fingerprint}
                      onSave={onSave}
                      onCancel={() => setNaming(null)}
                    />
                  </td>
                </tr>
              )}
            </Fragment>
          )
        })}
      </tbody>
    </table>
  )
}

/** The full 30-day picture, read-only — "as much of the RF data as possible". */
function FullTable({ devices }) {
  return (
    <table className="rf-table rf-table-full">
      <thead>
        <tr>
          <th>Device</th><th>Class</th><th className="rf-num">Visits</th>
          <th className="rf-num">Days</th><th>First seen</th><th>Last seen</th>
          <th>Typical hours</th>
        </tr>
      </thead>
      <tbody>
        {devices.map((d) => (
          <tr key={d.fingerprint} className={d.registered ? 'rf-row-mine' : ''}>
            <td>
              <div className="rf-dev-name">
                {d.registered && <span className="rf-mine-tick">◆</span>}
                {d.label || d.model}
              </div>
              <div className="rf-dev-fp">{d.fingerprint}</div>
            </td>
            <td><ClassBadge cls={d.class} /></td>
            <td className="rf-num">{d.visits}</td>
            <td className="rf-num">{d.days_seen}</td>
            <td className="rf-ago">{timeAgo(d.first_seen)}</td>
            <td className="rf-ago">{timeAgo(d.last_seen)}</td>
            <td className="rf-hours">{(d.typical_hours || []).map(hourLabel).join(', ') || '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function RfAround() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [naming, setNaming] = useState(null)
  const [busyFp, setBusyFp] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [flash, setFlash] = useState(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    const poll = () => api.rfDashboard()
      .then((d) => { if (mounted.current) { setData(d); setError(null) } })
      .catch((e) => { if (mounted.current) setError(e.message) })
    poll()
    const id = setInterval(poll, POLL_MS)
    return () => { mounted.current = false; clearInterval(id) }
  }, [])

  const refresh = () => {
    setRefreshing(true)
    setFlash(null)
    api.rfRefresh()
      .then((d) => { if (mounted.current) { setData(d); setError(null) } })
      .catch((e) => { if (mounted.current) setFlash({ kind: 'err', text: e.message }) })
      .finally(() => { if (mounted.current) setRefreshing(false) })
  }

  const saveLabel = (device) => {
    setBusyFp(device.fingerprint)
    return api.rfLabelDevice(device)
      .then((res) => {
        if (!mounted.current) return
        if (res.dashboard) setData(res.dashboard)
        else api.rfDashboard().then((d) => mounted.current && setData(d)).catch(() => {})
        setNaming(null)
        setFlash({ kind: 'ok', text: `Flagged "${device.name}" as yours.` })
      })
      .finally(() => { if (mounted.current) setBusyFp(null) })
  }

  if (error && !data) return <div className="rf-page rf-loading">{error}</div>
  if (!data) return <div className="rf-page rf-loading">Loading…</div>

  const { counts = {}, traffic = {}, alerts = [], devices = [], device_types: types = [] } = data
  const seen24 = devices
    .filter((d) => d.seen_last_24h)
    .sort((a, b) => (CLASS_ORDER.indexOf(a.class) - CLASS_ORDER.indexOf(b.class))
      || (visits24(b) - visits24(a)))
  const allSorted = [...devices].sort((a, b) => (CLASS_ORDER.indexOf(a.class) - CLASS_ORDER.indexOf(b.class))
    || (b.visits - a.visits))
  const busiest = (traffic.busiest_hours_local || []).map(hourLabel).join(', ')

  return (
    <div className="rf-page">
      <div className="rf-page-header">
        <h2>RF / Around the House</h2>
        <div className="rf-header-right">
          <FeedPill feed={data.feed} />
          <button type="button" className="cc-btn" onClick={refresh} disabled={refreshing}>
            {refreshing ? 'POLLING…' : 'REFRESH NODE'}
          </button>
        </div>
      </div>

      {flash && <p className={`rf-flash rf-flash-${flash.kind}`}>{flash.text}</p>}

      <div className="rf-stats">
        <Stat
          label="Vehicles · 24h"
          value={traffic.vehicle_visits_last_24h ?? 0}
          sub={traffic.vehicle_visits_per_day != null ? `vs ${traffic.vehicle_visits_per_day}/day usual` : null}
        />
        <Stat label="Mine" value={counts.own || 0} sub="registered sensors" tone="mine" />
        <Stat label="Neighbour" value={counts['fixture-neighbour'] || 0} sub="fixtures" tone="neighbour" />
        <Stat label="New / unknown" value={counts['unknown-new'] || 0} sub="not seen before" tone="new" />
        <Stat label="Watch" value={counts['vehicle-watch'] || 0} sub="recurring vehicle" tone="watch" />
        <Stat label="Busiest hours" value={busiest || '—'} sub="vehicle traffic" />
      </div>

      {alerts.length > 0 && (
        <section>
          <h3>Alerts</h3>
          <div className="rf-alerts">
            {alerts.map((a) => (
              <div key={a.key} className={`rf-alert rf-sev-${a.severity}`}>
                <span className="rf-alert-sev">{a.severity}</span>
                <span className="rf-alert-text">{a.text}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section>
        <h3>Seen in the last 24 hours</h3>
        <DeviceTable
          devices={seen24}
          types={types}
          naming={naming}
          setNaming={setNaming}
          onSave={saveLabel}
          busyFp={busyFp}
        />
        <p className="rf-hint">
          Flag anything that lives in your home so it registers as yours — a named device
          becomes class “Mine”. Vehicles show how often they came back today and the gap
          between visits.
        </p>
      </section>

      <section>
        <button type="button" className="rf-toggle" onClick={() => setShowAll((v) => !v)}>
          {showAll ? '▾' : '▸'} Everything the node has classified ({devices.length}, last {data.feed?.window_days || 30}d)
        </button>
        {showAll && <FullTable devices={allSorted} />}
      </section>
    </div>
  )
}
