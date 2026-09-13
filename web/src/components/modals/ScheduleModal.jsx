import { useEffect, useState } from 'react'
import { api } from '../../api'
import Modal from './Modal'

function formatLocal(iso) {
  try {
    return new Date(iso).toLocaleString('en-US', {
      weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

export default function ScheduleModal({ onClose }) {
  const [reminders, setReminders] = useState(null)
  const [weather, setWeather] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.scheduleReminders().then(setReminders).catch((e) => setError(e.message))
    api.weatherNow().then(setWeather).catch(() => {})
  }, [])

  const upcoming = (reminders || [])
    .slice()
    .sort((a, b) => new Date(a.due_at) - new Date(b.due_at))
    .slice(0, 10)
  const forecast = (weather?.forecast || []).filter((f) => !f.error && f.temperature != null).slice(0, 5)

  return (
    <Modal title="Schedule" onClose={onClose}>
      {error && <p className="modal-error">{error}</p>}
      {!error && reminders === null && <p className="modal-empty">Loading…</p>}
      {!error && reminders && (
        <div className="modal-section">
          <div className="modal-section-title">Upcoming ({upcoming.length})</div>
          {upcoming.length === 0 && <p className="modal-empty">Nothing on the schedule.</p>}
          {upcoming.length > 0 && (
            <ul className="modal-row-list">
              {upcoming.map((r) => (
                <li key={r.id} className="modal-row">
                  <span className="modal-row-label">{r.text}</span>
                  <span className="modal-row-value">{formatLocal(r.due_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {weather && weather.temperature != null && (
        <div className="modal-section">
          <div className="modal-section-title">Weather</div>
          <div className="modal-row">
            <span className="modal-row-label">Now</span>
            <span className="modal-row-value">
              {Math.round(weather.temperature)}{weather.temperature_unit || '°'} · {weather.condition || ''}
            </span>
          </div>
          {forecast.map((f, i) => (
            <div key={i} className="modal-row">
              <span className="modal-row-label">
                {f.datetime ? new Date(f.datetime).toLocaleDateString(undefined, { weekday: 'short' }) : `+${i}`}
              </span>
              <span className="modal-row-value">{Math.round(f.temperature)}° · {f.condition || ''}</span>
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}
