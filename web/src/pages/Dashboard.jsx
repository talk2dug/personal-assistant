import { useEffect, useState } from 'react'
import { api } from '../api'
import HeroOrb from '../components/HeroOrb'
import FinanceCard from '../components/dashboard/FinanceCard'
import GpuCard from '../components/dashboard/GpuCard'
import HeaderWeather from '../components/dashboard/HeaderWeather'
import IntelligenceFeed from '../components/dashboard/IntelligenceFeed'
import MediaCard from '../components/dashboard/MediaCard'
import NetworkStatusPill from '../components/dashboard/NetworkStatusPill'
import SshHealthPanel from '../components/dashboard/SshHealthPanel'
import WeatherStrip from '../components/dashboard/WeatherStrip'
import AgentsModal from '../components/modals/AgentsModal'
import CryptoModal from '../components/modals/CryptoModal'
import FinanceModal from '../components/modals/FinanceModal'
import MediaModal from '../components/modals/MediaModal'
import ScheduleModal from '../components/modals/ScheduleModal'
import { AgentsSection, CryptoSection, ReviewSection, ScheduleSection } from '../components/StatusPanel'
import { useJarvis } from '../context/JarvisContext'
import { useStatusPanel } from '../hooks/useStatusPanel'
import './command-center.css'

/**
 * The Command Center -- Jarvis's single landing screen (personal dashboard tasks: the
 * one-page redesign). Row-based card grid, hero centered in the first row, styled after
 * the owner's reference image: everything real that used to require navigating to a
 * separate page (agents, crypto, schedule, review, finance, media, SSH infra, GPU/
 * inference) now lives here as its own small card, each reusing an already-working data
 * source rather than re-fetching or re-deriving anything. Voice/text chat state itself
 * still lives entirely in JarvisContext, unchanged -- this page is a view onto it.
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
  // Which detail modal (if any) is open -- 'agents' | 'crypto' | 'schedule' | 'finance'
  // | 'media' | null. One piece of state for all five rather than five booleans, since
  // only one can ever be open at a time.
  const [openModal, setOpenModal] = useState(null)
  const now = useClock()
  const pendingReview = usePendingReviewCount()
  const {
    agents, agentsError,
    book, cryptoTraders, cryptoError,
    schedule, scheduleError,
    pendingReview: reviewCount, reviewError,
  } = useStatusPanel()

  return (
    <div className={`command-center ${focusMode ? 'is-focus' : ''}`}>
      <header className="cc-topbar">
        <div className="cc-topbar-left">
          <div className="cc-status-pill">
            <span className="cc-status-dot" />
            SYSTEM STATUS · OPTIMAL
          </div>
          <HeaderWeather />
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

      {!focusMode && (
        <div className="cc-grid">
          <div className="cc-row cc-row-hero">
            <div
              className="dash-panel cc-card-sm is-clickable"
              role="button" tabIndex={0}
              onClick={() => setOpenModal('agents')}
              onKeyDown={(e) => e.key === 'Enter' && setOpenModal('agents')}
            >
              <AgentsSection agents={agents} error={agentsError} />
            </div>
            <div className="cc-hero-row">
              <HeroOrb />
              <div className="cc-hero-caption">
                <span className="cc-hero-mode">{MODE_LABEL[mode] || mode}</span>
                <span className="cc-hero-text">{mediaError || caption}</span>
              </div>
            </div>
            <div className="dash-panel cc-card-md">
              <IntelligenceFeed />
            </div>
          </div>

          <div className="cc-row">
            <div
              className="dash-panel cc-card-sm is-clickable"
              role="button" tabIndex={0}
              onClick={() => setOpenModal('crypto')}
              onKeyDown={(e) => e.key === 'Enter' && setOpenModal('crypto')}
            >
              <CryptoSection book={book} hasTraders={cryptoTraders > 0} error={cryptoError} />
            </div>
            <div
              className="dash-panel cc-card-sm is-clickable"
              role="button" tabIndex={0}
              onClick={() => setOpenModal('schedule')}
              onKeyDown={(e) => e.key === 'Enter' && setOpenModal('schedule')}
            >
              <ScheduleSection schedule={schedule} error={scheduleError} />
            </div>
            <div className="dash-panel cc-card-sm">
              <ReviewSection pending={reviewCount} error={reviewError} />
            </div>
          </div>

          <div className="cc-row">
            <FinanceCard onClick={() => setOpenModal('finance')} />
            <MediaCard onClick={() => setOpenModal('media')} />
            <GpuCard />
          </div>

          <div className="cc-row cc-row-wide">
            <SshHealthPanel />
          </div>
        </div>
      )}

      {focusMode && (
        <div className="cc-hero-row cc-hero-row-focus">
          <HeroOrb />
          <div className="cc-hero-caption">
            <span className="cc-hero-mode">{MODE_LABEL[mode] || mode}</span>
            <span className="cc-hero-text">{mediaError || caption}</span>
          </div>
        </div>
      )}

      <footer className="cc-footer">
        <div className="cc-footer-side">
          <WeatherStrip />
          <NetworkStatusPill />
        </div>
        <button
          type="button"
          className={`cc-talk-button ${recording ? 'is-recording' : ''}`}
          onClick={toggleRecording}
          disabled={transcribing}
        >
          <span className="cc-talk-dot" />
          <span className="cc-talk-label">
            {recording ? 'Listening…' : transcribing ? 'Transcribing…' : 'Talk to Jarvis'}
          </span>
        </button>
        <div className="cc-footer-side cc-footer-side-right">
          <button type="button" className="footer-pill footer-link" onClick={() => setModalOpen(true)}>
            Message
          </button>
        </div>
      </footer>

      {openModal === 'agents' && <AgentsModal onClose={() => setOpenModal(null)} />}
      {openModal === 'crypto' && <CryptoModal onClose={() => setOpenModal(null)} />}
      {openModal === 'schedule' && <ScheduleModal onClose={() => setOpenModal(null)} />}
      {openModal === 'finance' && <FinanceModal onClose={() => setOpenModal(null)} />}
      {openModal === 'media' && <MediaModal onClose={() => setOpenModal(null)} />}
    </div>
  )
}
