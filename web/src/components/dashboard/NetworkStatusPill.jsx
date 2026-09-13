import { useEffect, useState } from 'react'
import { api } from '../../api'

const POLL_MS = 30000

// A real "is the network okay" signal derived from the same SSH host reachability data
// the health panel shows -- never a fabricated "Excellent" label. X/Y up, colored by
// whether everything registered actually answered.
export default function NetworkStatusPill() {
  const [hosts, setHosts] = useState(null)

  useEffect(() => {
    let cancelled = false
    function load() {
      api.sshHostsStatus().then((d) => !cancelled && setHosts(d.hosts || [])).catch(() => {})
    }
    load()
    const id = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  if (!hosts || hosts.length === 0) return null
  const up = hosts.filter((h) => h.reachable).length
  const allUp = up === hosts.length

  return (
    <span className={`footer-pill network-pill ${allUp ? 'is-ok' : 'is-degraded'}`}>
      <span className="footer-pill-dot" />
      Network {up}/{hosts.length}
    </span>
  )
}
