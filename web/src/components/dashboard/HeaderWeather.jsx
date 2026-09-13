import { useEffect, useState } from 'react'
import { api } from '../../api'

const POLL_MS = 10 * 60 * 1000

// A compact weekly strip for the top bar -- current temp plus the next few days, each
// just a weekday initial and a temperature. The full current-conditions + 5-day detail
// already lives on Schedule.jsx; this is deliberately smaller, a glance only.
export default function HeaderWeather() {
  const [weather, setWeather] = useState(null)

  useEffect(() => {
    let cancelled = false
    function load() {
      api.weatherNow().then((d) => !cancelled && setWeather(d)).catch(() => {})
    }
    load()
    const id = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  if (!weather || weather.temperature == null) return null
  const days = (weather.forecast || []).filter((f) => !f.error && f.temperature != null).slice(0, 5)

  return (
    <div className="cc-header-weather">
      <span className="cc-header-weather-now">
        {Math.round(weather.temperature)}{weather.temperature_unit || '°'}
      </span>
      {days.map((f, i) => (
        <span key={i} className="cc-header-weather-day">
          <span className="cc-header-weather-day-label">
            {f.datetime ? new Date(f.datetime).toLocaleDateString(undefined, { weekday: 'narrow' }) : ''}
          </span>
          {Math.round(f.temperature)}°
        </span>
      ))}
    </div>
  )
}
