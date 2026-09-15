import { useAgentStatus } from '../../hooks/useAgentStatus'
import { TINT } from '../../lib/cc'
import Panel, { PanelEmpty, PanelError } from './Panel'

const TOTAL_VRAM_GB = 24

/**
 * The inference bridge: how much of the card is free, who is on it, and what is loaded.
 *
 * Rides the roster's existing 4s poll rather than opening its own — the GPU block comes
 * back in the same /api/agents/status payload, so a second timer would be a second
 * request for data already in hand.
 */
export default function GpuPanel({ onOpen }) {
  const { status, error } = useAgentStatus()
  const gpu = status?.gpu

  const free = gpu?.vram_free_gb
  const pct = free != null ? Math.max(0, Math.min(100, (free / TOTAL_VRAM_GB) * 100)) : 0
  const state = gpu ? (gpu.mode === 'reserved' ? 'RESERVED' : gpu.reachable ? 'AVAILABLE' : 'OFFLINE') : '—'
  const stateTint = gpu?.mode === 'reserved' ? TINT.warn : gpu?.reachable ? TINT.ok : TINT.warn

  return (
    <Panel title="GPU / inference" meta={state} metaTint={stateTint} onOpen={onOpen}>
      {error && <PanelError>bridge unreachable — {error}</PanelError>}
      {!error && !gpu && <PanelEmpty>No GPU bridge configured.</PanelEmpty>}

      {!error && gpu && (
        <>
          <div className="cc-figure-row">
            <span className="cc-figure">{free != null ? free.toFixed(1) : '—'}</span>
            <span className="cc-figure-unit">GB VRAM free of {TOTAL_VRAM_GB}</span>
          </div>

          <div className="cc-meter">
            <span className="cc-meter-fill" style={{ width: `${pct}%`, background: TINT.base }} />
          </div>

          <div className="cc-statline">
            <span><b>{gpu.running ?? 0}</b> running</span>
            <span><b>{gpu.queued ?? 0}</b> queued</span>
            {gpu.mode && <span><b>{gpu.mode}</b> mode</span>}
          </div>

          <div className="cc-subtle">
            {gpu.loaded_models?.length
              ? `loaded · ${gpu.loaded_models.map((m) => m.model).join(' · ')}`
              : 'no models resident'}
          </div>
        </>
      )}
    </Panel>
  )
}
