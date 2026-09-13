import { useAgentStatus } from '../../hooks/useAgentStatus'

/**
 * The Command Center's "System Monitor" slot uses real numbers this app actually has --
 * the GPU/inference bridge's own status (VRAM, queue depth, loaded models) via the
 * shared useAgentStatus poll the Office page already drives its scene from -- rather
 * than fabricating CPU/RAM/disk gauges nothing in this codebase measures today.
 */
export default function GpuCard() {
  const { status, error } = useAgentStatus()
  const gpu = status?.gpu

  return (
    <section className={`dash-panel gpu-card ${gpu ? (gpu.mode === 'reserved' ? 'reserved' : gpu.reachable ? 'ok' : 'down') : ''}`}>
      <span className="hud-label">GPU / Inference</span>
      {error && <p className="dash-panel-error">{error}</p>}
      {!error && !gpu && <p className="dash-panel-loading">Not configured.</p>}
      {!error && gpu && (
        <>
          <div className="dash-card-figure">
            {gpu.mode === 'reserved' ? 'RESERVED' : gpu.reachable ? 'AVAILABLE' : 'OFFLINE'}
          </div>
          <div className="dash-card-sub">
            {gpu.running} running · {gpu.queued} queued
            {gpu.vram_free_gb != null && ` · ${gpu.vram_free_gb}GB free`}
          </div>
          {gpu.loaded_models?.length > 0 && (
            <div className="dash-card-sub">
              {gpu.loaded_models.map((m) => m.model).join(', ')}
            </div>
          )}
        </>
      )}
    </section>
  )
}
