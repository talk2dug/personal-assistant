import { useEffect, useState } from 'react'
import { api } from '../api'
import HeroOrb from '../components/HeroOrb'
import FinanceCard from '../components/dashboard/FinanceCard'
import GpuCard from '../components/dashboard/GpuCard'
import IntelligenceFeed from '../components/dashboard/IntelligenceFeed'
import MediaCard from '../components/dashboard/MediaCard'
import SshHealthPanel from '../components/dashboard/SshHealthPanel'
import WeatherStrip from '../components/dashboard/WeatherStrip'
import StatusPanel from '../components/StatusPanel'
import { useJarvis } from '../context/JarvisContext'
import './command-center.css'

/**
 * The Command Center -- Jarvis's single landing screen (personal dashboard tasks: the
 * one-page redesign). Replaces the old split between a mostly-empty Dashboard and a
 * Chat page whose only content was a big audio-reactive canvas: everything real that
 * used to require navigating to a separate page (agents, crypto, schedule, review,
 * finance, media, SSH infra, GPU/inference) now lives here as small cards, each reusing
 * an already-working data source (see StatusPanel.jsx/IntelligenceFeed.jsx and friends)
 * rather than re-fetching or re-deriving anything. Voice/text chat state itself still
 * lives entirely in JarvisContext, unchanged -- this page is a view onto it, same as
 * the old orb page was.
 *
 * Focus Mode collapses this down to just the hero + voice bar -- the old orb page's
 * whole experience, reachable as a state of this one page rather than a separate route.
 */

function useClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])
  return now
}

function usePendingReviewCount() {
  const [pending, setPending] = useState(0)
  useEffect(() => {
    let cancelled = false
    function load() {
      api.reviewItems('pending').then((d) => !cancelled && setPending(d.pending ?? 0)).catch(() => {})
    }
    load()
    const id = setInterval(load, 20000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])
  return pending
}

const MODE_LABEL = { idle: 'Standing by', listening: 'Listening', thinking: 'Thinking', speaking: 'Speaking' }

export default function Dashboard() {
  const { mode, caption, recording, transcribing, mediaError, toggleRecording, setModalOpen } = useJarvis()
  const [focusMode, setFocusMode] = useState(false)
  const now = useClock()
  const pendingReview = usePendingReviewCount()

  return (
    <div className={`command-center ${focusMode ? 'is-focus' : ''}`}>
      <header className="cc-topbar">
        <div className="cc-status-pill">
          <span className="cc-status-dot" />
          SYSTEM STATUS · OPTIMAL
        </div>
        <div className="cc-clock">
          {now.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}
          <span className="cc-clock-time">
            {now.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}
          </span>
        </div>
        <div className="cc-topbar-actions">
          {pendingReview > 0 && <span className="cc-review-badge">{pendingReview}</span>}
          <button type="button" className="cc-focus-toggle" onClick={() => setFocusMode((v) => !v)}>
            {focusMode ? 'Exit Focus' : 'Focus Mode'}
          </button>
        </div>
      </header>

      <div className="cc-hero-row">
        <HeroOrb size={focusMode ? 'large' : 'large'} />
        <div className="cc-hero-caption">
          <span className="cc-hero-mode">{MODE_LABEL[mode] || mode}</span>
          <span className="cc-hero-text">{mediaError || caption}</span>
        </div>
      </div>

      {!focusMode && (
        <div className="cc-grid">
          <StatusPanel />
          <IntelligenceFeed />
          <div className="cc-card-row">
            <FinanceCard />
            <MediaCard />
            <GpuCard />
          </div>
          <SshHealthPanel />
        </div>
      )}

      <footer className="cc-footer">
        <WeatherStrip />
        <button
          type="button"
          className={`cc-talk-button ${recording ? 'is-recording' : ''}`}
          onClick={toggleRecording}
          disabled={transcribing}
        >
          <span className="cc-talk-dot" />
          {recording ? 'Listening…' : transcribing ? 'Transcribing…' : 'Talk to Jarvis'}
        </button>
        <button type="button" className="footer-pill footer-link" onClick={() => setModalOpen(true)}>
          Message
        </button>
      </footer>
    </div>
  )
}
