import { useEffect, useRef } from 'react'
import { useJarvis } from '../context/JarvisContext'
import StatusPanel from '../components/StatusPanel'

/**
 * The orb: the largest view onto the Jarvis session.
 *
 * All session state (mic, camera, speech, transcript) now lives in JarvisContext above
 * the router, so this page owns nothing but the canvas visualiser (and, below it, a
 * compact status panel — see components/StatusPanel.jsx). Navigating away no longer
 * stops a recording or cuts off a reply mid-sentence — the orb just stops being the
 * thing you're looking at.
 */

const ACCENT = '#22e8ff'
const BAR_COUNT = 84

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
}

export default function Chat() {
  const {
    mode, caption, mediaError, recording, transcribing, cameraOn, voiceOn, voiceName,
    toggleRecording, toggleCamera, toggleVoice, setModalOpen,
    analyserRef, freqRef, modeRef,
  } = useJarvis()

  const canvasRef = useRef(null)
  const ampRef = useRef(new Float32Array(BAR_COUNT))
  const envRef = useRef(0)
  const t0Ref = useRef(performance.now())
  const rafRef = useRef(null)

  useEffect(() => {
    function ring(ctx, r, rot, color, alpha, dash, w) {
      ctx.save()
      ctx.rotate(rot)
      ctx.setLineDash(dash || [])
      ctx.lineWidth = w || 1
      ctx.strokeStyle = hexA(color, alpha)
      ctx.beginPath()
      ctx.arc(0, 0, r, 0, Math.PI * 2)
      ctx.stroke()
      ctx.restore()
    }

    function arc(ctx, r, rot, span, color, alpha, w) {
      ctx.save()
      ctx.rotate(rot)
      ctx.lineWidth = w
      ctx.lineCap = 'round'
      ctx.strokeStyle = hexA(color, alpha)
      ctx.beginPath()
      ctx.arc(0, 0, r, 0, span)
      ctx.stroke()
      ctx.restore()
    }

    function targetAmp(i, t, currentMode) {
      const p = i / BAR_COUNT
      if (currentMode === 'listening' && analyserRef.current && freqRef.current) {
        analyserRef.current.getByteFrequencyData(freqRef.current)
        const bins = freqRef.current.length
        const v = freqRef.current[Math.floor(Math.min(0.62, p < 0.5 ? p * 1.2 : (1 - p) * 1.2) * bins)] / 255
        return 0.08 + v * 1.5
      }
      if (currentMode === 'thinking') {
        const s1 = ((p - ((t * 0.22) % 1)) + 1) % 1
        const s2 = ((p - ((t * 0.22 + 0.5) % 1)) + 1) % 1
        const g = (d) => Math.exp(-Math.pow(Math.min(d, 1 - d) * 7, 2))
        return 0.07 + 0.75 * (g(s1) + 0.6 * g(s2))
      }
      if (currentMode === 'speaking') {
        const shape = Math.pow(1 - Math.abs(2 * p - 1), 0.55)
        return 0.1 + envRef.current * shape * 1.15
      }
      return 0.09 + 0.05 * Math.sin(t * 1.1 + p * Math.PI * 4)
    }

    function draw() {
      const c = canvasRef.current
      if (!c) {
        rafRef.current = requestAnimationFrame(draw)
        return
      }
      const size = Math.max(180, Math.round(c.clientWidth || 420))
      const k = size / 420
      const dpr = Math.min(2, window.devicePixelRatio || 1)
      if (c.width !== Math.round(size * dpr)) {
        c.width = Math.round(size * dpr)
        c.height = Math.round(size * dpr)
      }
      const ctx = c.getContext('2d')
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, size, size)
      ctx.translate(size / 2, size / 2)

      const t = (performance.now() - t0Ref.current) / 1000
      const currentMode = modeRef.current
      const speed = currentMode === 'idle' ? 0.5 : currentMode === 'thinking' ? 2.4 : 1

      const drive =
        currentMode === 'speaking'
          ? Math.max(0, 0.42 + 0.34 * Math.sin(t * 7.6) + 0.22 * Math.sin(t * 3.1 + 1.2) + 0.12 * Math.sin(t * 13.3))
          : 0
      envRef.current += (drive - envRef.current) * 0.22

      ring(ctx, 186 * k, t * 0.12 * speed, ACCENT, 0.16, [1, 9], 1)
      ring(ctx, 172 * k, -t * 0.18 * speed, ACCENT, 0.1, [34 * k, 18 * k], 1)
      ring(ctx, 114 * k, t * 0.05, ACCENT, 0.22, [], 1)
      arc(ctx, 198 * k, t * 0.55 * speed, 1.15, ACCENT, 0.5, 2)
      arc(ctx, 198 * k, t * 0.55 * speed + Math.PI, 0.42, ACCENT, 0.28, 2)
      arc(ctx, 158 * k, -t * 0.4 * speed, 0.75, ACCENT, 0.35, 3)

      for (let j = 0; j < 24; j++) {
        const a = (j / 24) * Math.PI * 2
        const long = j % 6 === 0
        ctx.strokeStyle = hexA(ACCENT, long ? 0.4 : 0.16)
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.moveTo(Math.cos(a) * 208 * k, Math.sin(a) * 208 * k)
        ctx.lineTo(Math.cos(a) * (long ? 219 : 213) * k, Math.sin(a) * (long ? 219 : 213) * k)
        ctx.stroke()
      }

      const amp = ampRef.current
      for (let i = 0; i < BAR_COUNT; i++) {
        const tv = targetAmp(i, t, currentMode)
        amp[i] += (tv - amp[i]) * 0.17
        const a = (i / BAR_COUNT) * Math.PI * 2 - Math.PI / 2
        const inner = 118 * k
        const len = (7 + Math.max(0, amp[i]) * 66) * k
        const ca = Math.cos(a)
        const sa = Math.sin(a)
        ctx.strokeStyle = hexA(ACCENT, 0.24 + Math.min(0.66, amp[i] * 0.9))
        ctx.lineWidth = 2.4 * Math.max(0.6, k)
        ctx.lineCap = 'round'
        ctx.beginPath()
        ctx.moveTo(ca * inner, sa * inner)
        ctx.lineTo(ca * (inner + len), sa * (inner + len))
        ctx.stroke()
      }

      if (currentMode === 'thinking') {
        for (let j = 0; j < 3; j++) {
          const a = t * 1.7 + (j * Math.PI * 2) / 3
          ctx.fillStyle = hexA(ACCENT, 0.85)
          ctx.beginPath()
          ctx.arc(Math.cos(a) * 148 * k, Math.sin(a) * 148 * 0.42 * k, 3.4 * Math.max(0.6, k), 0, Math.PI * 2)
          ctx.fill()
        }
      }

      rafRef.current = requestAnimationFrame(draw)
    }

    rafRef.current = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(rafRef.current)
  }, [analyserRef, freqRef, modeRef])

  return (
    <div className="orb-page">
      <div className="orb-header">
        <span className="hud-label">STATE / {mode.toUpperCase()}</span>
        <span className="hud-label">{voiceOn ? `VOICE / ${voiceName || 'DEFAULT'}` : 'VOICE / MUTED'}</span>
      </div>

      <div className="orb-canvas-wrap">
        <div className={`orb-core-glow orb-core-glow-${mode}`} />
        <canvas ref={canvasRef} className="orb-canvas" />
      </div>

      <div className="orb-caption">{caption}</div>
      <div className="orb-hint">
        Press <kbd>space</kbd> to talk — tap to latch, hold to push-to-talk, <kbd>esc</kbd> to cancel
      </div>

      <StatusPanel />

      {mediaError && <div className="chat-media-error">{mediaError}</div>}

      <div className="chat-toolbar orb-toolbar">
        <button
          type="button"
          className={`toolbar-btn ${recording ? 'recording' : ''}`}
          onClick={toggleRecording}
          disabled={transcribing}
        >
          <span className="dot" />
          {recording ? 'listening…' : transcribing ? 'transcribing…' : 'mic'}
        </button>
        <button type="button" className={`toolbar-btn ${cameraOn ? 'active' : ''}`} onClick={toggleCamera}>
          <span className="dot" />
          {cameraOn ? 'cam on' : 'cam'}
        </button>
        <button type="button" className={`toolbar-btn ${voiceOn ? 'active' : ''}`} onClick={toggleVoice}>
          <span className="dot" />
          {voiceOn ? 'voice on' : 'voice off'}
        </button>
        <button type="button" className="toolbar-btn" onClick={() => setModalOpen(true)}>
          <span className="dot" />
          chat
        </button>
      </div>
    </div>
  )
}
