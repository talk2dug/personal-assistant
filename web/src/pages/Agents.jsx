import { useEffect, useRef, useState } from 'react'
import {
  COLS, DECOR, DESKS, FURNITURE_FILES, ROWS, blockedTiles, walkableTiles,
} from '../office/layout'
import { applyServerState, createCharacter, renderOffice, setDecor, updateCharacter } from '../office/engine'
import { TILE, loadAssets } from '../office/sprites'
import { AGENT_STATUS_LABEL as STATUS_LABEL, useAgentStatus } from '../hooks/useAgentStatus'

export default function Agents() {
  const canvasRef = useRef(null)
  const charactersRef = useRef([])
  const assetsRef = useRef(null)
  const gpuRef = useRef(null)
  const rafRef = useRef(0)
  const { status, error } = useAgentStatus()
  const [scale, setScale] = useState(3)

  // Drives the animated office from the shared poll above (useAgentStatus, 4s): seeds
  // characters once, then reconciles positions/state against the server on every
  // subsequent tick. Deliberately separate from the animation loop below: the office
  // keeps moving at 60fps between polls, so a 4s refresh looks continuous rather than
  // like a slideshow.
  useEffect(() => {
    if (!status) return
    gpuRef.current = status.gpu
    if (!charactersRef.current.length) {
      charactersRef.current = status.agents.map((a, i) => createCharacter(i, a))
    }
    applyServerState(charactersRef.current, status.agents, status.gpu?.jobs || [])
  }, [status])

  useEffect(() => {
    let cancelled = false
    setDecor(DECOR)
    // floor_0 is the seamless plain tile; the others carry grout lines or brickwork that
    // tile into a busy grid and fight the sprites.
    loadAssets({ characters: 6, floors: ['floor_0'], furniture: FURNITURE_FILES }).then((assets) => {
      if (!cancelled) assetsRef.current = assets
    })
    return () => { cancelled = true }
  }, [])

  // Fit the office to the viewport at a whole-number scale — a fractional scale on pixel
  // art produces uneven, shimmering edges.
  useEffect(() => {
    function fit() {
      const available = Math.min(window.innerWidth - 80, 1400)
      setScale(Math.max(2, Math.min(4, Math.floor(available / (COLS * TILE)))))
    }
    fit()
    window.addEventListener('resize', fit)
    return () => window.removeEventListener('resize', fit)
  }, [])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const blocked = blockedTiles()
    const walkable = walkableTiles(blocked)
    let last = performance.now()

    function frame(now) {
      const dt = Math.min((now - last) / 1000, 0.1)
      last = now
      if (assetsRef.current) {
        charactersRef.current.forEach((ch) => updateCharacter(ch, dt, blocked, walkable))
        renderOffice(ctx, assetsRef.current, charactersRef.current, gpuRef.current, scale, now / 1000)
      }
      rafRef.current = requestAnimationFrame(frame)
    }
    rafRef.current = requestAnimationFrame(frame)
    return () => cancelAnimationFrame(rafRef.current)
  }, [scale])

  const gpu = status?.gpu
  const agents = status?.agents || []
  const pipeline = status?.pipeline || {}

  return (
    <div className="office-page">
      <header className="office-header">
        <div>
          <h1>The Office</h1>
          <p className="office-sub">
            {status?.agents_scheduled
              ? 'Agents are on schedule.'
              : 'Agents run on demand only — nothing is scheduled.'}
          </p>
        </div>
        {gpu && (
          <div className={`gpu-chip ${gpu.mode === 'reserved' ? 'reserved' : gpu.reachable ? 'ok' : 'down'}`}>
            <span className="gpu-chip-title">GPU BRIDGE</span>
            <span className="gpu-chip-state">
              {gpu.mode === 'reserved'
                ? `RESERVED${gpu.reason ? ` · ${gpu.reason}` : ''}`
                : gpu.reachable ? 'AVAILABLE' : 'OFFLINE'}
            </span>
            <span className="gpu-chip-meta">
              {gpu.running} running · {gpu.queued} queued
              {gpu.vram_free_gb != null && ` · ${gpu.vram_free_gb}GB free`}
            </span>
          </div>
        )}
      </header>

      {error && <div className="office-error">Couldn’t reach the office: {error}</div>}

      <div className="office-stage">
        <canvas
          ref={canvasRef}
          className="office-canvas"
          width={COLS * TILE * scale}
          height={ROWS * TILE * scale}
        />
      </div>

      <div className="office-panels">
        <section className="office-panel">
          <h2>Staff</h2>
          <ul className="staff-list">
            {agents.map((a) => (
              <li key={a.key} className={`staff-row status-${a.status}`}>
                <span className="staff-pip" />
                <span className="staff-name">{a.title}</span>
                <span className="staff-status">{STATUS_LABEL[a.status] || a.status}</span>
                <span className="staff-detail">{a.detail}</span>
              </li>
            ))}
          </ul>
        </section>

        <section className="office-panel">
          <h2>GPU queue</h2>
          {gpu?.jobs?.length ? (
            <ul className="queue-list">
              {gpu.jobs.map((j) => (
                <li key={j.id} className={`queue-row ${j.status}`}>
                  <span className="queue-job">#{j.id}</span>
                  <span className="queue-agent">{j.agent}</span>
                  <span className="queue-task">{j.task_type}</span>
                  <span className="queue-state">{j.status}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="office-empty">Nobody’s waiting. The machine is free.</p>
          )}
          {gpu?.loaded_models?.length > 0 && (
            <p className="office-note">
              Loaded: {gpu.loaded_models.map((m) => `${m.model} (${m.vram_gb}GB)`).join(', ')}
            </p>
          )}
        </section>

        <section className="office-panel">
          <h2>In tray</h2>
          <ul className="tray-list">
            {[
              ['Market leads', pipeline.market_leads],
              ['Trend ideas', pipeline.trend_leads],
              ['Product concepts', pipeline.concepts],
              ['Art briefs', pipeline.briefs],
              ['Listings', pipeline.listings],
              ['Social posts', pipeline.posts],
            ].map(([label, n]) => (
              <li key={label} className={n ? 'has-items' : ''}>
                <span>{label}</span>
                <span className="tray-count">{n ?? 0}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  )
}
