import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import Console from '../components/cc/Console'
import CryptoDesk from '../components/cc/CryptoDesk'
import DeviceMesh from '../components/cc/DeviceMesh'
import EventLog from '../components/cc/EventLog'
import GpuPanel from '../components/cc/GpuPanel'
import Header from '../components/cc/Header'
import NetWorth from '../components/cc/NetWorth'
import Pipelines from '../components/cc/Pipelines'
import Presence from '../components/cc/Presence'
import Readouts from '../components/cc/Readouts'
import Roster from '../components/cc/Roster'
import SchedulePanel from '../components/cc/SchedulePanel'
import SectionModal from '../components/cc/SectionModal'
import TasksPanel from '../components/cc/TasksPanel'
import WorkInFlight from '../components/cc/WorkInFlight'
import CameraWall from '../components/cc/CameraWall'
import { useCommandCenter } from '../hooks/useCommandCenter'
import './command-center.css'

const WEATHER_MS = 10 * 60 * 1000
// Past this long without a successful snapshot, the header stops claiming everything is
// nominal. A board that says "ALL SYSTEMS NOMINAL" while disconnected is worse than one
// that says nothing at all.
const STALE_MS = 20000

/**
 * The Command Center — the whole application, on one screen.
 *
 * Three columns, densest in the middle where the orb and the conversation are:
 *
 *   left   — the machine: who is working, what the GPU is doing, which hosts answer,
 *            and what the house is up to.
 *   centre — Jarvis himself, the work in flight, what Jack owes the day, and the two
 *            standing pipelines.
 *   right  — the money, the calendar, and the running log of what actually happened.
 *
 * Every panel is a door. Clicking one opens that section as a modal over the board
 * (SectionModal) rather than navigating away, so the conversation and the live polling
 * never stop. There is no nav bar and no router: the board is the app.
 */
export default function CommandCenter() {
  const {
    snapshot, snapshotError, lastUpdate,
    work, hosts, finance, netWorth, crypto, schedule, slowErrors,
  } = useCommandCenter()

  const [weather, setWeather] = useState(null)
  const [section, setSection] = useState(null)
  const [camerasOpen, setCamerasOpen] = useState(false)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    let cancelled = false
    const load = () => api.weatherNow().then((d) => !cancelled && setWeather(d)).catch(() => {})
    load()
    const id = setInterval(load, WEATHER_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  // Drives the staleness check and the schedule panel's has-this-passed test -- the
  // header keeps its own second-by-second clock for the display time.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 5000)
    return () => clearInterval(id)
  }, [])

  const openSection = useCallback((key) => setSection(key), [])
  const closeSection = useCallback(() => setSection(null), [])

  const pendingReview = Number(
    (snapshot?.readouts || []).find((r) => r.label === 'Review queue')?.value ?? 0,
  )
  const stale = !!snapshotError || (lastUpdate ? now - lastUpdate.getTime() > STALE_MS : false)

  return (
    <div className="command-center">
      <div className="cc-grid-wash" aria-hidden="true" />
      <div className="cc-scanline" aria-hidden="true" />

      <Header
        weather={weather}
        pendingReview={pendingReview}
        onOpenSection={openSection}
        lastUpdate={lastUpdate}
        stale={stale}
      />

      <div className="cc-board">
        <div className="cc-col cc-col-left">
          <Roster onOpen={() => openSection('agents')} />
          <GpuPanel onOpen={() => openSection('agents')} />
          <DeviceMesh hosts={hosts} error={slowErrors.hosts} onOpen={() => openSection('agents')} />
          <Presence
            home={snapshot?.home}
            onOpen={() => openSection('kitchen')}
            onOpenCameras={() => setCamerasOpen(true)}
          />
        </div>

        <div className="cc-col cc-col-mid">
          <Console pendingReview={pendingReview} />

          <div className="cc-row">
            <WorkInFlight items={work} error={slowErrors.work} onOpen={() => openSection('review')} />
            <TasksPanel tasks={snapshot?.tasks} onOpen={() => openSection('tasks')} />
          </div>

          <Pipelines
            pipelines={snapshot?.pipelines}
            onOpenBusiness={() => openSection('media')}
            onOpenDev={() => openSection('review')}
          />
        </div>

        <div className="cc-col cc-col-right">
          <NetWorth
            finance={finance}
            history={netWorth}
            error={slowErrors.finance || slowErrors.netWorth}
            onOpen={() => openSection('finance')}
          />
          <CryptoDesk crypto={crypto} error={slowErrors.crypto} onOpen={() => openSection('crypto')} />
          <SchedulePanel
            schedule={schedule}
            error={slowErrors.schedule}
            now={now}
            onOpen={() => openSection('schedule')}
          />
          <EventLog events={snapshot?.events} onOpen={() => openSection('review')} />
        </div>
      </div>

      <Readouts readouts={snapshot?.readouts} onOpen={openSection} />

      {snapshotError && (
        <div className="cc-banner">
          Board data stale — {snapshotError}. Showing the last good read.
        </div>
      )}

      {section && <SectionModal sectionKey={section} onClose={closeSection} />}
      {camerasOpen && (
        <CameraWall cameras={snapshot?.home?.cameras?.list} onClose={() => setCamerasOpen(false)} />
      )}
    </div>
  )
}
