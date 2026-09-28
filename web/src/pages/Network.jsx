import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import './network.css'

// The scheduler records host and Omada checks every 15 min; this only needs to feel
// current while the modal is open.
const POLL_MS = 30000

function parseTs(iso) {
  if (!iso) return null
  const norm = iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`
  const d = new Date(norm)
  return Number.isNaN(d.getTime()) ? null : d
}

function timeAgo(iso) {
  const d = parseTs(iso)
  if (!d) return '—'
  const s = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function when(iso) {
  const d = parseTs(iso)
  if (!d) return '—'
  const today = new Date().toDateString() === d.toDateString()
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  return today ? time : `${d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })} ${time}`
}

// A down host whose finding says the check itself is suspect is shown as amber, not red:
// the machine is fine, the probe is wrong. Amber still means "wants Jack".
function hostTone(h) {
  if (h.reachable) return 'ok'
  return h.finding?.kind === 'check_suspect' ? 'warn' : 'bad'
}
const HOST_LABEL = { ok: 'up', warn: 'check suspect', bad: 'down' }

const PLAN_TONE = { succeeded: 'ok', proposed: 'warn', running: 'info', failed: 'bad' }
const UNNAMED = /^([0-9A-F]{2}[-:]){5}[0-9A-F]{2}$|^(wlan0|lwip0|esp-[0-9a-f]+)$/i

/** What deserves Jack's eye first, derived only from recorded facts. */
function attentionItems(data) {
  const items = []
  for (const h of data.hosts) {
    if (h.reachable) continue
    const tone = hostTone(h)
    items.push({
      key: `host-${h.name}`, tone,
      pill: tone === 'warn' ? 'check suspect' : `down ${timeAgo(h.last_down_at).replace(' ago', '')}`,
      title: `${h.name} failing its ${h.kind} check`,
      body: `${h.consecutive_failures} failed checks in a row, first at ${when(h.last_down_at)}. `
        + `It dials ${h.target || 'an unconfigured address'}.`,
      finding: h.finding?.text,
    })
  }
  for (const d of data.omada.devices.filter((x) => !x.online)) {
    items.push({
      key: `dev-${d.mac}`, tone: 'bad', pill: 'offline',
      title: `${d.name || d.model} is offline`,
      body: `${d.model} at ${d.ip || 'no IP'} dropped off the controller at ${when(d.last_offline_at)}.`,
    })
  }
  for (const c of data.omada.clients.filter((x) => x.is_new)) {
    items.push({
      key: `new-${c.mac}`, tone: 'info', pill: 'new device',
      title: `${UNNAMED.test(c.name || '') ? 'Unnamed device' : c.name} joined the network`,
      body: `MAC ${c.mac} at ${c.ip || 'no IP'}, first seen ${when(c.first_seen_at)}. `
        + 'Block it from the Omada controller if you don\'t recognise it.',
    })
  }
  const proposed = data.plans.filter((p) => p.status === 'proposed')
  if (proposed.length) {
    items.push({
      key: 'plans', tone: 'warn', pill: `${proposed.length} waiting`,
      title: `Ops plan${proposed.length > 1 ? 's' : ''} ${proposed.map((p) => `#${p.id}`).join(', ')} awaiting approval`,
      body: 'The sys-admin agent drafted these and won\'t run them without you. Approve or reject in the Review queue.',
    })
  }
  const rank = { bad: 0, warn: 1, info: 2 }
  return items.sort((a, b) => rank[a.tone] - rank[b.tone])
}

function Pill({ tone, children }) {
  return <span className={`net-pill net-${tone}`}>{children}</span>
}

function Status({ tone, children }) {
  return <span className={`net-status net-${tone}`}>{children}</span>
}

function Summary({ data }) {
  const hostsUp = data.hosts.filter((h) => h.reachable).length
  const devs = data.omada.devices
  const devsUp = devs.filter((d) => d.online).length
  const clients = data.omada.clients
  const clientsUp = clients.filter((c) => c.online).length
  const newCount = clients.filter((c) => c.is_new).length
  const proposed = data.plans.filter((p) => p.status === 'proposed').length

  const tiles = [
    { label: 'Host checks passing', value: hostsUp, of: data.hosts.length,
      note: `${data.hosts.length - hostsUp} failing`, warn: hostsUp < data.hosts.length },
    { label: 'Omada devices online', value: devsUp, of: devs.length,
      note: devs.map((d) => d.model?.split(' ')[0]).filter(Boolean).join(' · ') || '—', warn: devsUp < devs.length },
    { label: 'Clients online', value: clientsUp, of: clients.length,
      note: newCount ? `${newCount} new in 24h` : 'none new in 24h', warn: newCount > 0 },
    { label: 'Plans awaiting you', value: proposed, of: null,
      note: proposed ? 'in the Review queue' : 'nothing to approve', warn: proposed > 0 },
  ]
  return (
    <div className="net-tiles">
      {tiles.map((t) => (
        <div key={t.label} className="net-tile">
          <div className="net-eyebrow">{t.label}</div>
          <div className={`net-big ${t.warn ? 'net-big-warn' : ''}`}>
            {t.value}{t.of != null && <small>/{t.of}</small>}
          </div>
          <div className="net-muted">{t.note}</div>
        </div>
      ))}
    </div>
  )
}

function Attention({ items }) {
  if (!items.length) {
    return <p className="net-muted">Nothing needs you. Every check is passing and no plans are waiting.</p>
  }
  return (
    <div className="net-alerts">
      {items.map((a) => (
        <div key={a.key} className={`net-alert net-${a.tone}`}>
          <div className="net-alert-top">
            <Pill tone={a.tone}>{a.pill}</Pill>
            <span className="net-alert-title">{a.title}</span>
          </div>
          <p>{a.body}</p>
          {a.finding && <p className="net-finding">{a.finding}</p>}
        </div>
      ))}
    </div>
  )
}

function OmadaDevices({ omada }) {
  if (!omada.configured) {
    return <p className="net-muted">Omada isn't configured. Add the controller's Open API credentials to config.json.</p>
  }
  if (!omada.devices.length) {
    return <p className="net-muted">No devices recorded yet. The Omada check runs every 15 minutes.</p>
  }
  const order = { gateway: 0, switch: 1, ap: 2 }
  const devices = [...omada.devices].sort((a, b) => (order[a.kind] ?? 9) - (order[b.kind] ?? 9))
  return (
    <div className="net-devices">
      {devices.map((d) => (
        <div key={d.mac} className={`net-device ${d.kind === 'ap' ? 'net-device-ap' : ''}`}>
          <span className={`net-dot ${d.online ? '' : 'net-dot-bad'}`} />
          <div className="net-device-main">
            <div className="net-device-name">{d.name || d.model}</div>
            <div className="net-muted">{d.kind} · {d.model}</div>
          </div>
          <div className="net-device-side">
            <div className="net-mono">{d.ip || '—'}</div>
            <div className="net-muted">
              {d.online
                ? (d.last_offline_at ? `back since ${when(d.last_online_at)}` : 'no drops recorded')
                : `offline ${timeAgo(d.last_offline_at)}`}
            </div>
          </div>
        </div>
      ))}
      <p className="net-muted net-caption">
        Controller {omada.controller_url || '—'} · polled {timeAgo(omada.checked_at)}.
        Rebooting a device goes through chat and needs your yes.
      </p>
    </div>
  )
}

function HostTable({ hosts }) {
  const rank = { bad: 0, warn: 1, ok: 2 }
  const sorted = [...hosts].sort((a, b) => rank[hostTone(a)] - rank[hostTone(b)] || a.name.localeCompare(b.name))
  return (
    <div className="net-table-wrap">
      <table className="net-table">
        <thead>
          <tr><th>Host</th><th>Check</th><th>Status</th><th className="net-num">Latency</th><th>Last down</th></tr>
        </thead>
        <tbody>
          {sorted.map((h) => {
            const tone = hostTone(h)
            return (
              <tr key={h.name}>
                <td>
                  <div className="net-host-name">{h.name}</div>
                  <div className="net-mono net-faint">
                    {h.omada_ip && h.omada_ip !== h.target ? `${h.target} → ${h.omada_ip}` : (h.target || '')}
                  </div>
                </td>
                <td className="net-kind">{h.kind}</td>
                <td><Status tone={tone}>{HOST_LABEL[tone]}</Status></td>
                <td className={`net-num net-mono ${h.latency_ms >= 300 ? 'net-slow' : ''}`}>
                  {h.latency_ms == null ? '—' : `${h.latency_ms} ms`}
                </td>
                <td className="net-mono net-faint">{h.last_down_at ? when(h.last_down_at) : 'never'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

const CLIENT_FILTERS = [
  ['all', 'All'], ['fleet', 'Checked hosts'], ['unnamed', 'Unnamed'], ['new', 'New'], ['offline', 'Offline'],
]

function Clients({ clients, hosts }) {
  const [filter, setFilter] = useState('all')
  const [q, setQ] = useState('')
  const hostByIp = useMemo(() => {
    const m = {}
    for (const h of hosts) if (h.omada_ip) m[h.omada_ip] = h.name
    return m
  }, [hosts])

  const rows = clients
    .filter((c) => {
      if (filter === 'fleet') return !!hostByIp[c.ip]
      if (filter === 'unnamed') return UNNAMED.test(c.name || '')
      if (filter === 'new') return c.is_new
      if (filter === 'offline') return !c.online
      return true
    })
    .filter((c) => {
      const s = q.trim().toLowerCase()
      return !s || [c.name, c.ip, c.mac, hostByIp[c.ip]].some((v) => (v || '').toLowerCase().includes(s))
    })

  return (
    <>
      <div className="net-controls">
        <input
          id="net-client-search" type="search" className="net-search"
          placeholder="name, IP or MAC" value={q} onChange={(e) => setQ(e.target.value)}
          aria-label="Filter clients"
        />
        <div className="net-seg" role="group" aria-label="Show clients">
          {CLIENT_FILTERS.map(([k, label]) => (
            <button key={k} type="button" aria-pressed={filter === k} onClick={() => setFilter(k)}>{label}</button>
          ))}
        </div>
        <span className="net-muted">{rows.length} of {clients.length}</span>
      </div>
      <div className="net-table-wrap net-clients">
        <table className="net-table">
          <thead><tr><th>Name</th><th>IP</th><th>MAC</th><th>Status</th><th>First seen</th></tr></thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.mac}>
                <td>
                  {c.name || '—'}
                  {c.is_new && <span className="net-tag">new</span>}
                  {hostByIp[c.ip] && <span className="net-faint"> · {hostByIp[c.ip]}</span>}
                </td>
                <td className="net-mono">{c.ip || '—'}</td>
                <td className="net-mono net-faint">{c.mac}</td>
                <td>
                  <Status tone={c.online ? 'ok' : 'warn'}>
                    {c.online ? 'online' : `offline · ${timeAgo(c.last_seen_at)}`}
                  </Status>
                </td>
                <td className="net-mono net-faint">{when(c.first_seen_at)}</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={5} className="net-faint">No clients match.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  )
}

function Plans({ plans }) {
  if (!plans.length) return <p className="net-muted">The sys-admin agent hasn't drafted any ops plans yet.</p>
  return (
    <div className="net-plans">
      {plans.map((p) => (
        <details key={p.id} className="net-plan">
          <summary>
            <span className="net-mono net-faint">#{p.id}</span>
            <Pill tone={PLAN_TONE[p.status] || 'info'}>{p.status}</Pill>
            <span className="net-plan-summary">{p.summary}</span>
            <span className="net-mono net-faint net-plan-when">{when(p.created_at)}</span>
          </summary>
          <ol className="net-steps">
            {p.steps.map((s) => (
              <li key={s.step_index}>
                <div className="net-step-head">
                  <span className="net-kind">{s.phase}</span>
                  <span className="net-mono">{s.host}</span>
                  <Status tone={PLAN_TONE[s.status] || (s.status === 'skipped' ? 'info' : 'warn')}>{s.status}</Status>
                </div>
                <div>{s.purpose}</div>
                {s.output && <pre className="net-output">{s.output}</pre>}
              </li>
            ))}
          </ol>
        </details>
      ))}
    </div>
  )
}

/**
 * Network — every host check, the Omada network's own view, and what the sys-admin agent
 * has tried. The join between the first two is what makes it useful: a check that's
 * failing while Omada sees the machine online is a broken check, not a dead box.
 */
export default function Network() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(() => api.infraNetwork()
    .then((d) => { setData(d); setError(null) })
    .catch((e) => setError(e.message || String(e))), [])

  useEffect(() => {
    load()
    const id = setInterval(load, POLL_MS)
    return () => clearInterval(id)
  }, [load])

  const attention = useMemo(() => (data ? attentionItems(data) : []), [data])

  if (!data) {
    return <div className="net-page net-loading">{error ? `Couldn't load the network: ${error}` : 'Reading the network…'}</div>
  }

  return (
    <div className="net-page">
      <div className="net-header">
        <h2>Network</h2>
        <div className="net-feeds">
          <span className="net-feed">hosts checked {timeAgo(data.hosts_checked_at)}</span>
          <span className="net-feed">omada polled {timeAgo(data.omada.checked_at)}</span>
          {error && <span className="net-feed net-feed-stale">refresh failed, showing last read</span>}
        </div>
      </div>

      <Summary data={data} />

      <section>
        <h3>Needs attention</h3>
        <Attention items={attention} />
      </section>

      <div className="net-split">
        <section>
          <h3>Omada infrastructure</h3>
          <OmadaDevices omada={data.omada} />
        </section>
        <section>
          <h3>Host checks</h3>
          <HostTable hosts={data.hosts} />
        </section>
      </div>

      <section>
        <h3>Clients on the network</h3>
        <Clients clients={data.omada.clients} hosts={data.hosts} />
        {data.omada.tracking_since && (
          <p className="net-muted net-caption">
            Client tracking began {when(data.omada.tracking_since)}. Devices from that first sweep were already on the network.
          </p>
        )}
      </section>

      <section>
        <h3>Sys-admin ops plans</h3>
        <Plans plans={data.plans} />
      </section>
    </div>
  )
}
