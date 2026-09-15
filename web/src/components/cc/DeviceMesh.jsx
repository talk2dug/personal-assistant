import { TINT } from '../../lib/cc'
import Panel, { PanelEmpty, PanelError } from './Panel'

/**
 * Every machine on the network Jarvis knows how to reach — not just the ones running
 * his own code. Reachability is a plain TCP probe (ssh_health.py), so "up" here means
 * the port answered, not that anything is healthy on the other side of it.
 */
export default function DeviceMesh({ hosts, error, onOpen }) {
  const list = hosts || []
  const up = list.filter((h) => h.reachable).length

  return (
    <Panel
      title="Device mesh"
      meta={list.length ? `${up}/${list.length} up` : null}
      metaTint={list.length && up < list.length ? TINT.warn : TINT.dim}
      onOpen={onOpen}
      className="cc-flexi"
    >
      {error && <PanelError>host check failed — {error}</PanelError>}
      {!error && hosts === null && <PanelEmpty>Probing hosts…</PanelEmpty>}
      {!error && hosts && list.length === 0 && <PanelEmpty>No hosts registered.</PanelEmpty>}

      <div className="cc-scrollbody">
        {list.map((host) => (
        <div key={host.name} className="cc-mesh-row">
          <span className="cc-pip" style={{ background: host.reachable ? TINT.base : TINT.warn }} />
          <span className="cc-mesh-name">{host.name}</span>
          <span className="cc-mesh-purpose">{host.purpose}</span>
          <span className="cc-mesh-state" style={{ color: host.reachable ? TINT.base : TINT.warn }}>
            {host.reachable ? `up · ${host.latency_ms}ms` : 'unreachable'}
          </span>
        </div>
        ))}
      </div>
    </Panel>
  )
}
