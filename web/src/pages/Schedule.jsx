import { useEffect, useState } from 'react'
import { api } from '../api'

function formatLocal(iso) {
  try {
    return new Date(iso).toLocaleString('en-US', {
      weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

/** Local datetime input wants "YYYY-MM-DDTHH:MM" with no timezone/seconds. */
function toDatetimeLocalDefault() {
  const d = new Date(Date.now() + 60 * 60 * 1000) // an hour from now, a sane default
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function ReminderForm({ onChange }) {
  const [show, setShow] = useState(false)
  const [text, setText] = useState('')
  const [dueAt, setDueAt] = useState(toDatetimeLocalDefault())
  const [scope, setScope] = useState('private')

  async function handleCreate(e) {
    e.preventDefault()
    if (!text.trim() || !dueAt) return
    await api.createReminder({ text: text.trim(), due_at: dueAt, scope })
    setText('')
    setDueAt(toDatetimeLocalDefault())
    setShow(false)
    onChange()
  }

  return (
    <>
      <div className="reminders-header">
        <h3>Reminders</h3>
        <button onClick={() => setShow((s) => !s)}>{show ? 'Cancel' : '+ Add reminder'}</button>
      </div>
      {show && (
        <form className="reminder-form" onSubmit={handleCreate}>
          <input placeholder="What's the reminder?" value={text} onChange={(e) => setText(e.target.value)} />
          <input type="datetime-local" value={dueAt} onChange={(e) => setDueAt(e.target.value)} />
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="private">Just me</option>
            <option value="shared">Shared</option>
          </select>
          <button type="submit">Save</button>
        </form>
      )}
    </>
  )
}

function RemindersList() {
  const [reminders, setReminders] = useState(null)

  async function load() {
    setReminders(await api.scheduleReminders())
  }

  useEffect(() => { load() }, [])

  async function handleDelete(id) {
    await api.deleteReminder(id)
    load()
  }

  if (reminders === null) return <p className="empty-hint">Loading…</p>

  return (
    <div className="reminders-list">
      <ReminderForm onChange={load} />
      {reminders.length === 0 && <p className="empty-hint">Nothing on the schedule.</p>}
      <ul>
        {reminders.map((r) => (
          <li key={r.id} className="reminder-row">
            <div>
              <div className="reminder-text">{r.text}</div>
              <div className="reminder-meta">
                {formatLocal(r.due_at)}
                {r.scope === 'shared' && <span className="reminder-shared-tag"> · shared</span>}
              </div>
            </div>
            <button className="reminder-delete" onClick={() => handleDelete(r.id)}>✕</button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function WeatherPanel() {
  const [weather, setWeather] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.weatherNow().then(setWeather).catch((e) => setError(e.message))
  }, [])

  if (error) return <p className="empty-hint">Weather unavailable — {error}</p>
  if (!weather) return <p className="empty-hint">Loading…</p>

  const forecast = (weather.forecast || []).filter((f) => !f.error).slice(0, 5)

  return (
    <div className="weather-panel">
      <div className="weather-now">
        <div className="weather-temp">
          {weather.temperature != null ? Math.round(weather.temperature) : '—'}
          <span className="weather-unit">{weather.temperature_unit || '°'}</span>
        </div>
        <div className="weather-condition">{weather.condition || 'unknown'}</div>
        <div className="weather-details">
          {weather.humidity != null && <span>{weather.humidity}% humidity</span>}
          {weather.wind_speed != null && <span>{weather.wind_speed} {weather.wind_speed_unit} wind</span>}
        </div>
      </div>
      {forecast.length > 0 && (
        <div className="weather-forecast">
          {forecast.map((f, i) => (
            <div key={i} className="weather-forecast-day">
              <div className="weather-forecast-date">
                {f.datetime ? new Date(f.datetime).toLocaleDateString('en-US', { weekday: 'short' }) : `+${i}`}
              </div>
              <div className="weather-forecast-temp">
                {f.temperature != null ? Math.round(f.temperature) : '—'}°
              </div>
              <div className="weather-forecast-cond">{f.condition || ''}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Schedule() {
  return (
    <div className="schedule-page">
      <section>
        <RemindersList />
      </section>
      <section>
        <h3>Weather</h3>
        <WeatherPanel />
      </section>
    </div>
  )
}
