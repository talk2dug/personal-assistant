import { useEffect, useRef } from 'react'
import { useJarvis } from '../context/JarvisContext'

/**
 * The Command Center's centerpiece -- rings, arcs, and instrument-panel tick marks
 * around a glowing core, colored and paced by the current mode (idle/listening/
 * thinking/speaking). Extracted from the old Chat.jsx orb, with one deliberate cut:
 * the 84-bar audio-frequency spectrum around the ring is gone. A decorative hero is
 * worth keeping (the owner asked for one), a jittery equalizer reading live mic/speech
 * levels as individual bars is the specific thing that read as "goofy" -- speaking mode
 * now gets a single smooth breathing pulse on the innermost arc instead (still reactive
 * to envRef's speech envelope, just not 84 discrete numbers fighting for attention).
 */

const ACCENT = '#22e8ff'

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
}

export default function HeroOrb({ size = 'large' }) {
  const { mode, analyserRef, freqRef, modeRef } = useJarvis()
  const canvasRef = useRef(null)
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

    function draw() {
      const c = canvasRef.current
      if (!c) {
        rafRef.current = requestAnimationFrame(draw)
        return
      }
      const boxSize = Math.max(140, Math.round(c.clientWidth || 340))
      const k = boxSize / 420
      const dpr = Math.min(2, window.devicePixelRatio || 1)
      if (c.width !== Math.round(boxSize * dpr)) {
        c.width = Math.round(boxSize * dpr)
        c.height = Math.round(boxSize * dpr)
      }
      const ctx = c.getContext('2d')
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, boxSize, boxSize)
      ctx.translate(boxSize / 2, boxSize / 2)

      const t = (performance.now() - t0Ref.current) / 1000
      const currentMode = modeRef.current
      const speed = currentMode === 'idle' ? 0.5 : currentMode === 'thinking' ? 2.4 : 1

      // A live mic/speech amplitude, smoothed into one number -- drives the breathing
      // pulse below rather than per-bar heights.
      let drive = 0
      if (currentMode === 'listening' && analyserRef.current && freqRef.current) {
        analyserRef.current.getByteFrequencyData(freqRef.current)
        const bins = freqRef.current.length
        let sum = 0
        for (let i = 0; i < bins; i++) sum += freqRef.current[i]
        drive = sum / bins / 255
      } else if (currentMode === 'speaking') {
        drive = Math.max(0, 0.42 + 0.34 * Math.sin(t * 7.6) + 0.22 * Math.sin(t * 3.1 + 1.2))
      }
      envRef.current += (drive - envRef.current) * 0.15

      ring(ctx, 186 * k, t * 0.12 * speed, ACCENT, 0.16, [1, 9], 1)
      ring(ctx, 172 * k, -t * 0.18 * speed, ACCENT, 0.1, [34 * k, 18 * k], 1)
      ring(ctx, 114 * k, t * 0.05, ACCENT, 0.22, [], 1)
      arc(ctx, 198 * k, t * 0.55 * speed, 1.15, ACCENT, 0.5, 2)
      arc(ctx, 198 * k, t * 0.55 * speed + Math.PI, 0.42, ACCENT, 0.28, 2)
      arc(ctx, 158 * k, -t * 0.4 * speed, 0.75, ACCENT, 0.3 + envRef.current * 0.35, 3 + envRef.current * 2.5)

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
    <div className={`hero-orb hero-orb-${size}`}>
      <div className={`orb-core-glow orb-core-glow-${mode}`} />
      <canvas ref={canvasRef} className="orb-canvas" />
    </div>
  )
}
