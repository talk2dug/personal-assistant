import { useEffect, useState } from 'react'
import { api } from '../../api'

const POLL_MS = 10 * 60 * 1000

export default function WeatherStrip() {
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

  return (
    <span className="footer-pill">
      {Math.round(weather.temperature)}{weather.temperature_unit || '°'} {weather.condition || ''}
    </span>
  )
}
