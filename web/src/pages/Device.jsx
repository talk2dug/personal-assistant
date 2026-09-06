import { useEffect, useRef, useState } from 'react'

/**
 * The screen on a voice terminal.
 *
 * Display only — it never touches the microphone. The Python client on the Pi owns all
 * audio (two processes fighting over one capture device is the kind of thing that works
 * on the bench and fails at 2am), so this page just polls that device's reported state
 * and draws it.
 *
 * Rendered outside the app shell: no nav, no login. A terminal on a shelf has nobody to
 * sign it in, so it authenticates with the device key in the URL, which is also what the
 * kiosk browser is launched with.
 */

const ACCENT = '#22e8ff'
const BAR_COUNT = 72
const POLL_MS = 700

const CAPTION = {
  idle: 'Say “hey Jarvis”.',
  listening: 'Listening…',
  thinking: 'One moment.',
  speaking: '',
  offline: 'Waiting for the terminal…',
}

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
}

export default function Device() {
  const params = new URLSearchParams(window.location.search)
  const deviceId = params.get('id') || 'touch1'
  const key = params.get('key') || ''

  const [device, setDevice] = useState({ state: 'offline', caption: '' })
  const stateRef = useRef('offline')
  const canvasRef = useRef(null)
  const ampRef = useRef(new Float32Array(BAR_COUNT))
  const envRef = useRef(0)
  const t0Ref = useRef(performance.now())

  useEffect(() => {
    let cancelled = false
    let timer
    async function poll() {
      try {
        const res = await fetch(`/api/devices/${deviceId}?key=${encodeURIComponent(key)}`)
        if (res.ok && !cancelled) {
          const d = await res.json()
          setDevice(d)
          stateRef.current = d.state
        }
      } catch {
        // A terminal must not show an error page because one poll missed; the next one
        // will very likely succeed.
      }
      if (!cancelled) timer = setTimeout(poll, POLL_MS)
    }
    poll()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [deviceId, key])

  // Frames per second by state. The Pi has no working GPU acceleration for this canvas,
  // so every frame is rasterised on the CPU -- measured at ~79% of a core on Touch1 when
  // this ran flat out at 60fps. A terminal is idle almost all of the time, and the idle
  // animation is a slow rotation that reads identically at 10fps. The lively states keep
  // a high rate because that is when someone is actually looking at it.
  const FPS_BY_STATE = { idle: 10, offline: 4, listening: 30, thinking: 30, speaking: 30 }

  useEffect(() => {
    let raf
    let lastFrame = 0
    function draw(now) {
      const c = canvasRef.current
      if (!c) { raf = requestAnimationFrame(draw); return }

      // Throttle to the rate this state needs. requestAnimationFrame still fires at the
      // display rate; we simply decline to redraw, which costs nothing.
      const fps = FPS_BY_STATE[stateRef.current] ?? 30
      if (now && lastFrame && now - lastFrame < 1000 / fps - 1) {
        raf = requestAnimationFrame(draw)
        return
      }
      lastFrame = now || performance.now()

      const size = Math.min(c.clientWidth, c.clientHeight) || 320
      const dpr = Math.min(2, window.devicePixelRatio || 1)
      if (c.width !== Math.round(size * dpr)) {
        c.width = Math.round(size * dpr)
        c.height = Math.round(size * dpr)
      }
      const ctx = c.getContext('2d')
      const k = size / 420
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, size, size)
      ctx.translate(size / 2, size / 2)

      const t = (performance.now() - t0Ref.current) / 1000
      const mode = stateRef.current
      const speed = mode === 'idle' ? 0.5 : mode === 'thinking' ? 2.4 : 1
      const drive = mode === 'speaking'
        ? Math.max(0, 0.42 + 0.34 * Math.sin(t * 7.6) + 0.22 * Math.sin(t * 3.1 + 1.2))
        : 0
      envRef.current += (drive - envRef.current) * 0.22

      const dim = mode === 'offline' ? 0.3 : 1
      const ring = (r, rot, alpha, dash, w) => {
        ctx.save(); ctx.rotate(rot); ctx.setLineDash(dash || []); ctx.lineWidth = w || 1
        ctx.strokeStyle = hexA(ACCENT, alpha * dim)
        ctx.beginPath(); ctx.arc(0, 0, r, 0, Math.PI * 2); ctx.stroke(); ctx.restore()
      }
      ring(186 * k, t * 0.12 * speed, 0.16, [1, 9], 1)
      ring(172 * k, -t * 0.18 * speed, 0.1, [34 * k, 18 * k], 1)
      ring(114 * k, t * 0.05, 0.22, [], 1)

      const amp = ampRef.current
      for (let i = 0; i < BAR_COUNT; i++) {
        const p = i / BAR_COUNT
        let target
        if (mode === 'listening') {
          // No mic here, so listening is a calm travelling wave rather than a fake level
          // meter — pretending to show audio we don't have would be a lie on a screen.
          target = 0.1 + 0.5 * Math.pow(Math.max(0, Math.sin(p * Math.PI * 2 - t * 3)), 6)
        } else if (mode === 'thinking') {
          const s = ((p - ((t * 0.22) % 1)) + 1) % 1
          const g = (d) => Math.exp(-Math.pow(Math.min(d, 1 - d) * 7, 2))
          target = 0.07 + 0.75 * g(s)
        } else if (mode === 'speaking') {
          target = 0.1 + envRef.current * Math.pow(1 - Math.abs(2 * p - 1), 0.55) * 1.15
        } else {
          target = 0.09 + 0.05 * Math.sin(t * 1.1 + p * Math.PI * 4)
        }
        amp[i] += (target - amp[i]) * 0.17
        const a = p * Math.PI * 2 - Math.PI / 2
        const inner = 118 * k
        const len = (7 + Math.max(0, amp[i]) * 66) * k
        ctx.strokeStyle = hexA(ACCENT, (0.24 + Math.min(0.66, amp[i] * 0.9)) * dim)
        ctx.lineWidth = 2.4 * Math.max(0.6, k)
        ctx.lineCap = 'round'
        ctx.beginPath()
        ctx.moveTo(Math.cos(a) * inner, Math.sin(a) * inner)
        ctx.lineTo(Math.cos(a) * (inner + len), Math.sin(a) * (inner + len))
        ctx.stroke()
      }
      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [])

  const caption = device.caption || CAPTION[device.state] || ''

  return (
    <div className={`device-screen state-${device.state}`}>
      <div className="device-top">
        <span className="device-name">{device.name || deviceId}</span>
        <span className={`device-dot ${device.online ? 'online' : 'offline'}`} />
      </div>
      <div className="device-orb">
        <div className={`device-glow glow-${device.state}`} />
        <canvas ref={canvasRef} />
      </div>
      <div className="device-caption">{caption}</div>
    </div>
  )
}
