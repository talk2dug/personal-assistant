import { useEffect, useRef, useState } from 'react'
import { useJarvis } from '../../context/JarvisContext'

/**
 * The orb — the one element on the board allowed to leave the steel palette, because
 * its colour carries Jarvis's state rather than a status.
 *
 * The old HeroOrb was a <canvas> that redrew every ring on every frame. This is SVG,
 * which buys two things that matter here: the slow rotations and the breathing core
 * are CSS animations the compositor owns (so they keep moving smoothly no matter what
 * React is doing), and only the 56 spectrum bars are re-rendered from JS.
 *
 * The bars are the honest bit. When the mic is open they are real FFT magnitudes from
 * the live analyser JarvisContext already owns — the orb genuinely reacts to your
 * voice, it is not a loop pretending to listen. When the mic is closed they fall back
 * to a standing wave whose amplitude and speed come from the current mood, so idle
 * motion reads as "alive" rather than as "receiving a signal that isn't there".
 */

const MOODS = {
  idle: {
    label: 'Standing by', sub: 'all quiet',
    core: '#7fd8ff', mid: '#2a7fb8', ring: 'rgba(127,216,255,.24)',
    arc: '#7fd8ff', arc2: '#4e9fd6', fill: 'rgba(127,216,255,.08)', amp: 0.3, speed: 0.06,
  },
  listening: {
    label: 'Listening', sub: 'mic hot · capturing',
    core: '#35e3ff', mid: '#0f86ad', ring: 'rgba(53,227,255,.3)',
    arc: '#35e3ff', arc2: '#8be9ff', fill: 'rgba(53,227,255,.1)', amp: 1, speed: 0.22,
  },
  thinking: {
    label: 'Thinking', sub: 'reasoning',
    core: '#a97bff', mid: '#5b32b5', ring: 'rgba(169,123,255,.3)',
    arc: '#a97bff', arc2: '#d7b4ff', fill: 'rgba(169,123,255,.1)', amp: 0.62, speed: 0.3,
  },
  speaking: {
    label: 'Speaking', sub: 'voice out',
    core: '#6f7bff', mid: '#3239b5', ring: 'rgba(111,123,255,.3)',
    arc: '#6f7bff', arc2: '#b0b6ff', fill: 'rgba(111,123,255,.1)', amp: 0.85, speed: 0.18,
  },
  alert: {
    label: 'Needs you', sub: 'decision pending',
    core: '#e8a33d', mid: '#8a5a12', ring: 'rgba(232,163,61,.3)',
    arc: '#e8a33d', arc2: '#ffd79a', fill: 'rgba(232,163,61,.1)', amp: 0.5, speed: 0.12,
  },
}

/** The single source of truth for Jarvis's current mood — used by the orb and by the
 *  caption beside it, so the colour and the word can never drift apart. */
export function moodKeyFor({ mode, recording, pendingReview = 0 }) {
  if (recording) return 'listening'
  if (mode === 'thinking' || mode === 'speaking') return mode
  if (pendingReview > 0) return 'alert'
  return 'idle'
}

const BARS = 56
const TICKS = 40
const CX = 110
const CY = 110

function polar(angle, radius) {
  return [CX + Math.cos(angle) * radius, CY + Math.sin(angle) * radius]
}

/** Static tick ring — recomputed only when the mood's colours change. */
function ticks(mood) {
  return Array.from({ length: TICKS }, (_, i) => {
    const a = (i / TICKS) * Math.PI * 2
    const long = i % 5 === 0
    const [x1, y1] = polar(a, 112)
    const [x2, y2] = polar(a, long ? 124 : 118)
    return { x1, y1, x2, y2, tint: long ? mood.arc : mood.ring }
  })
}

export default function Orb({ size = 212, pendingReview = 0 }) {
  const { mode, recording, analyserRef, freqRef } = useJarvis()
  const [phase, setPhase] = useState(0)
  // Live mic magnitudes, one per bar. Accumulated in a ref (so the smoothing has
  // somewhere to live between frames) and published to state once per frame, because
  // render must never read a ref -- it would be free to show a stale frame.
  const levelsRef = useRef(new Float32Array(BARS))
  const [levels, setLevels] = useState(null)
  const rafRef = useRef(0)

  // Something waiting on a decision outranks idle: the orb turns amber and says so,
  // which is the whole reason the board has a mood at all.
  const mood = MOODS[moodKeyFor({ mode, recording, pendingReview })] || MOODS.idle

  useEffect(() => {
    let last = 0
    function frame(t) {
      rafRef.current = requestAnimationFrame(frame)
      // ~18fps for the bars. Faster buys nothing visible on a waveform this small and
      // this screen is expected to stay open for days.
      if (t - last < 55) return
      last = t

      const analyser = analyserRef?.current
      const freq = freqRef?.current
      const levels = levelsRef.current

      if (analyser && freq) {
        analyser.getByteFrequencyData(freq)
        // The useful speech energy sits in the low half of the spectrum; mapping all
        // 128 bins across the ring would spend half the orb rendering near-silence.
        const usable = Math.floor(freq.length * 0.55)
        for (let i = 0; i < BARS; i += 1) {
          const bin = Math.floor((i / BARS) * usable)
          const next = freq[bin] / 255
          // Attack fast, release slow — otherwise every bar snaps to zero between
          // syllables and the ring flickers instead of pulsing.
          levels[i] = next > levels[i] ? next : levels[i] * 0.82 + next * 0.18
        }
        setLevels(Array.from(levels))
      } else {
        // Mic closed: publish null once rather than an array of zeroes every frame, so
        // the standing-wave fallback takes over and React stops diffing 56 values.
        setLevels((prev) => (prev === null ? prev : null))
      }
      setPhase((p) => p + 1)
    }
    rafRef.current = requestAnimationFrame(frame)
    return () => cancelAnimationFrame(rafRef.current)
  }, [analyserRef, freqRef])

  const bars = Array.from({ length: BARS }, (_, i) => {
    const a = (i / BARS) * Math.PI * 2
    let height
    if (levels) {
      height = 6 + levels[i] * 46
    } else {
      // Three detuned sine components, so the standing wave never visibly repeats.
      const w = Math.sin(a * 3 + phase * mood.speed) * 0.55
        + Math.sin(a * 7 - phase * mood.speed * 0.6) * 0.3
        + Math.sin(a * 13 + phase * mood.speed * 1.4) * 0.15
      height = 6 + Math.abs(w) * 30 * mood.amp
    }
    const [x1, y1] = polar(a, 58)
    const [x2, y2] = polar(a, 58 + height)
    return { x1, y1, x2, y2, tint: height > 20 ? mood.core : mood.arc2 }
  })

  const coreR = 13 + Math.sin(phase * 0.12) * 2.2 * (0.4 + mood.amp)
  const tickRing = ticks(mood)
  const uid = 'orb'

  return (
    <div className="cc-orb" style={{ width: size }}>
      <svg viewBox="0 0 220 220" style={{ width: size, height: size, overflow: 'visible' }} aria-hidden="true">
        <defs>
          <radialGradient id={`${uid}Core`}>
            <stop offset="0%" stopColor={mood.core} stopOpacity="0.95" />
            <stop offset="42%" stopColor={mood.mid} stopOpacity="0.34" />
            <stop offset="100%" stopColor={mood.mid} stopOpacity="0" />
          </radialGradient>
          <filter id={`${uid}Glow`} x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="2.6" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        <circle cx={CX} cy={CY} r="104" fill={`url(#${uid}Core)`} className="orb-breathe" />

        <g className="orb-spin-slow">
          <circle cx={CX} cy={CY} r="104" fill="none" stroke={mood.ring} strokeWidth="1" strokeDasharray="2 9" />
          <path d="M110 6 A104 104 0 0 1 196 58" fill="none" stroke={mood.arc}
                strokeWidth="2.4" filter={`url(#${uid}Glow)`} />
          <path d="M110 214 A104 104 0 0 1 24 162" fill="none" stroke={mood.arc2}
                strokeWidth="2" opacity="0.75" />
        </g>

        <g className="orb-spin-rev">
          <circle cx={CX} cy={CY} r="90" fill="none" stroke={mood.ring} strokeWidth="1" strokeDasharray="30 16" />
          <circle cx={CX} cy="20" r="2.6" fill={mood.arc2} />
        </g>

        <g>
          {tickRing.map((t, i) => (
            <line key={i} x1={t.x1.toFixed(1)} y1={t.y1.toFixed(1)}
                  x2={t.x2.toFixed(1)} y2={t.y2.toFixed(1)} stroke={t.tint} strokeWidth="1" />
          ))}
        </g>

        <g filter={`url(#${uid}Glow)`}>
          {bars.map((b, i) => (
            <line key={i} x1={b.x1.toFixed(1)} y1={b.y1.toFixed(1)}
                  x2={b.x2.toFixed(1)} y2={b.y2.toFixed(1)}
                  stroke={b.tint} strokeWidth="1.6" strokeLinecap="round" />
          ))}
        </g>

        <g className="orb-spin-fast">
          <path d="M110 56 A54 54 0 0 1 158 86" fill="none" stroke={mood.arc}
                strokeWidth="3" strokeLinecap="round" filter={`url(#${uid}Glow)`} />
          <path d="M110 164 A54 54 0 0 1 62 134" fill="none" stroke={mood.arc2}
                strokeWidth="3" strokeLinecap="round" opacity="0.8" />
        </g>

        <circle cx={CX} cy={CY} r="38" fill={mood.fill} stroke={mood.arc}
                strokeWidth="1" className="orb-breathe-fast" />
        <circle cx={CX} cy={CY} r={coreR.toFixed(1)} fill={mood.core}
                filter={`url(#${uid}Glow)`} opacity="0.9" />
      </svg>
    </div>
  )
}

export { MOODS }
