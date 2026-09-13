import { useEffect, useState } from 'react'
import { api } from '../../api'

// Every registered SSH host, not just Jarvis's own machines -- ssh_health.py does a
// plain TCP reachability check (no login), merged with the is_jarvis_host/purpose
// metadata the same registry already exposes to the LLM's list_ssh_hosts tool.
const POLL_MS = 30000

export default function SshHealthPanel() {
  const [hosts, setHosts] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    function load() {
      api.sshHostsStatus().then((d) => !cancelled && (setHosts(d.hosts || []), setError(null)))
        .catch((e) => !cancelled && setError(e.message))
    }
    load()
    const id = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return (
    <section className="dash-panel">
      <span className="hud-label">SSH Hosts</span>
      {error && <p className="dash-panel-error">{error}</p>}
      {!error && hosts === null && <p className="dash-panel-loading">Loading…</p>}
      {!error && hosts && hosts.length === 0 && <p className="dash-panel-loading">No hosts registered.</p>}
      {!error && hosts && hosts.length > 0 && (
        <ul className="ssh-host-list">
          {hosts.map((h) => (
            <li key={h.name} className={`ssh-host-row ${h.reachable ? 'is-up' : 'is-down'}`}>
              <span className="ssh-host-pip" />
              <span className="ssh-host-name">{h.name}</span>
              {h.is_jarvis_host && <span className="ssh-host-badge">JARVIS</span>}
              <span className="ssh-host-state">
                {h.reachable ? `up · ${h.latency_ms}ms` : 'unreachable'}
              </span>
              <span className="ssh-host-purpose">{h.purpose}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
