import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './homepanel.css'

/**
 * Home control on a touch terminal: every light and switch, split by room, beside the orb.
 *
 * Tap a tile to toggle it; press and hold a light for the full sheet (brightness, white
 * temperature, colour, and the individual bulbs when it's a group). Rooms and what's in
 * them come from Home Assistant's areas via /api/devices/home/rooms, so a new device
 * shows up here once it has an area -- nothing to edit in this file.
 *
 * Authenticated with the same device key as the rest of the kiosk page.
 */

const ROOMS_POLL_MS = 2500
const INFO_POLL_MS = 60000
const HOLD_MS = 450
// How long a tap's optimistic state wins over polled state. HA can take a beat to report
// a change back; without this a tile flickers on, off (stale poll), then on again.
const OPTIMISTIC_MS = 4000
const SEND_THROTTLE_MS = 300
const SCENES = '__scenes__'

const PRESETS = [
  { label: 'Warm', kelvin: 2700 },
  { label: 'Soft', kelvin: 3300 },
  { label: 'Neutral', kelvin: 4000 },
  { label: 'Day', kelvin: 6000 },
  { label: 'Red', hs: [0, 100] },
  { label: 'Orange', hs: [28, 100] },
  { label: 'Yellow', hs: [52, 100] },
  { label: 'Green', hs: [120, 90] },
  { label: 'Teal', hs: [175, 90] },
  { label: 'Blue', hs: [225, 100] },
  { label: 'Purple', hs: [275, 90] },
  { label: 'Pink', hs: [320, 80] },
]

// Inline SVG, not emoji: the Pi terminals have no emoji font and drew every icon as a box.
const PATHS = {
  bulb: 'M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2V16h5v-.1c0-.8.4-1.5 1-2A6 6 0 0 0 12 3z',
  power: 'M12 3v9M7.05 6.05a7 7 0 1 0 9.9 0',
  fan: 'M12 12c0-4 1-8 4-8s2 5-4 8zm0 0c4 0 8 1 8 4s-5 2-8-4zm0 0c0 4-1 8-4 8s-2-5 4-8zm0 0c-4 0-8-1-8-4s5-2 8 4z',
  music: 'M9 18V5l11-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm11-2a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
  scene: 'M12 3l2.2 5.8L20 11l-5.8 2.2L12 19l-2.2-5.8L4 11l5.8-2.2z',
  car: 'M5 16h14M6 16v2M18 16v2M4 16v-4l2-5h12l2 5v4M4 12h16M7.5 14h.01M16.5 14h.01',
  sun: 'M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM12 1v3M12 20v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1',
  moon: 'M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z',
  cloud: 'M7 18h10a4 4 0 0 0 .5-8A6 6 0 0 0 6 9.5 4.3 4.3 0 0 0 7 18z',
  partly: 'M8 3v1.5M3.5 8H2M4.8 4.8l1 1M12.5 7.5a4.5 4.5 0 0 0-8.3 2.3M8 19h9a3.5 3.5 0 0 0 .4-7A5.5 5.5 0 0 0 7 12.3 3.4 3.4 0 0 0 8 19z',
  rain: 'M7 14h10a4 4 0 0 0 .5-8A6 6 0 0 0 6 5.5 4.3 4.3 0 0 0 7 14zM8 17l-1 3M12 17l-1 3M16 17l-1 3',
  storm: 'M7 14h10a4 4 0 0 0 .5-8A6 6 0 0 0 6 5.5 4.3 4.3 0 0 0 7 14zM13 14l-3 4h4l-3 4',
  snow: 'M7 14h10a4 4 0 0 0 .5-8A6 6 0 0 0 6 5.5 4.3 4.3 0 0 0 7 14zM8 18h.01M12 20h.01M16 18h.01M10 22h.01M14 22h.01',
  fog: 'M3 9h18M5 13h14M3 17h18',
  wind: 'M3 8h11a3 3 0 1 0-3-3M3 12h16a3 3 0 1 1-3 3M3 16h8',
}
function Icon({ name, className = '' }) {
  return (
    <svg className={`hp-icon ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name] || PATHS.power} />
    </svg>
  )
}
const WEATHER_ICON = {
  sunny: 'sun', 'clear-night': 'moon', partlycloudy: 'partly', cloudy: 'cloud', rainy: 'rain',
  pouring: 'rain', lightning: 'storm', 'lightning-rainy': 'storm', snowy: 'snow', 'snowy-rainy': 'snow',
  fog: 'fog', windy: 'wind', 'windy-variant': 'wind', hail: 'snow', exceptional: 'storm',
}
const WEATHER_LABEL = {
  sunny: 'Sunny', 'clear-night': 'Clear', partlycloudy: 'Partly cloudy', cloudy: 'Cloudy',
  rainy: 'Rain', pouring: 'Heavy rain', lightning: 'Storms', 'lightning-rainy': 'Storms',
  snowy: 'Snow', 'snowy-rainy': 'Sleet', fog: 'Fog', windy: 'Windy', 'windy-variant': 'Windy',
  hail: 'Hail', exceptional: 'Alert',
}

function kelvinToRgb(k) {
  // Tanner Helland's approximation -- close enough to tint a tile.
  const t = k / 100
  const r = t <= 66 ? 255 : 329.7 * Math.pow(t - 60, -0.1332)
  const g = t <= 66 ? 99.47 * Math.log(t) - 161.12 : 288.12 * Math.pow(t - 60, -0.0755)
  const b = t >= 66 ? 255 : t <= 19 ? 0 : 138.52 * Math.log(t - 10) - 305.04
  const c = (v) => Math.max(0, Math.min(255, Math.round(v)))
  return [c(r), c(g), c(b)]
}

function tileColor(t) {
  if (t.state !== 'on') return null
  if (t.color_mode === 'color_temp' && t.color_temp_kelvin) return kelvinToRgb(t.color_temp_kelvin)
  if (Array.isArray(t.rgb_color)) return t.rgb_color
  if (t.domain === 'light') return [255, 196, 120]
  return null
}

function stateText(t) {
  if (!t.available) return 'Unavailable'
  if (t.domain === 'scene') return 'Scene'
  if (t.domain === 'media_player') return t.media_title || t.state
  if (t.state !== 'on') return 'Off'
  if (t.domain === 'light' && t.brightness_pct != null) return `${t.brightness_pct}%`
  if (t.domain === 'fan' && t.percentage != null) return `${t.percentage}%`
  return 'On'
}

const ICON = { light: 'bulb', switch: 'power', fan: 'fan', media_player: 'music', scene: 'scene' }

function useHold(onTap, onHold) {
  const timer = useRef(null)
  const held = useRef(false)
  const start = useRef(null)
  const cancel = () => { clearTimeout(timer.current); timer.current = null }
  return {
    onPointerDown: (e) => {
      held.current = false
      start.current = { x: e.clientX, y: e.clientY }
      cancel()
      timer.current = setTimeout(() => { held.current = true; timer.current = null; onHold && onHold() }, HOLD_MS)
    },
    onPointerMove: (e) => {
      // A finger scrolling the grid is not a press.
      if (start.current && Math.hypot(e.clientX - start.current.x, e.clientY - start.current.y) > 12) {
        cancel(); start.current = null
      }
    },
    onPointerUp: () => {
      if (timer.current && start.current) { cancel(); onTap && onTap() }
      start.current = null
    },
    onPointerCancel: () => { cancel(); start.current = null },
    onPointerLeave: () => { cancel(); start.current = null },
    onContextMenu: (e) => e.preventDefault(),
  }
}

function Tile({ tile, onTap, onHold }) {
  const handlers = useHold(onTap, onHold)
  const rgb = tileColor(tile)
  const style = rgb ? { '--tile-rgb': rgb.join(',') } : undefined
  return (
    <button
      type="button"
      className={`hp-tile ${tile.state === 'on' ? 'on' : ''} ${tile.available ? '' : 'unavailable'} ${tile.group ? 'group' : ''}`}
      style={style}
      {...handlers}
    >
      <span className="hp-tile-icon"><Icon name={ICON[tile.domain]} /></span>
      <span className="hp-tile-name">{tile.name}</span>
      <span className="hp-tile-state">{tile.group ? `All · ${stateText(tile)}` : stateText(tile)}</span>
      {tile.state === 'on' && tile.brightness_pct != null && (
        <span className="hp-tile-bar" style={{ width: `${tile.brightness_pct}%` }} />
      )}
    </button>
  )
}

function Slider({ label, value, min, max, step = 1, className = '', onInput, onCommit, format }) {
  const [local, setLocal] = useState(value)
  const dragging = useRef(false)
  useEffect(() => { if (!dragging.current) setLocal(value) }, [value])
  return (
    <label className={`hp-slider ${className}`}>
      <span className="hp-slider-head">
        <span>{label}</span>
        <span className="hp-slider-value">{format ? format(local) : local}</span>
      </span>
      <input
        type="range" min={min} max={max} step={step} value={local ?? min}
        onPointerDown={() => { dragging.current = true }}
        onInput={(e) => { const v = Number(e.target.value); setLocal(v); onInput && onInput(v) }}
        onChange={(e) => { const v = Number(e.target.value); setLocal(v) }}
        onPointerUp={(e) => { dragging.current = false; onCommit && onCommit(Number(e.target.value)) }}
        onKeyUp={(e) => onCommit && onCommit(Number(e.target.value))}
      />
    </label>
  )
}

function LightSheet({ tile, index, send, openOther, onClose, onBack }) {
  const lastSent = useRef(0)
  const pendingTimer = useRef(null)
  const throttled = (service, data) => {
    const now = Date.now()
    clearTimeout(pendingTimer.current)
    if (now - lastSent.current >= SEND_THROTTLE_MS) {
      lastSent.current = now
      send(tile.entity_id, service, data)
    } else {
      pendingTimer.current = setTimeout(() => {
        lastSent.current = Date.now()
        send(tile.entity_id, service, data)
      }, SEND_THROTTLE_MS)
    }
  }
  const commit = (service, data) => { clearTimeout(pendingTimer.current); send(tile.entity_id, service, data) }
  const hs = Array.isArray(tile.hs_color) ? tile.hs_color : [30, 60]
  const hue = Math.round(hs[0])
  const sat = Math.round(hs[1])
  const members = (tile.members || []).map((id) => index[id]).filter(Boolean)
  const isOn = tile.state === 'on'

  return (
    <div className="hp-sheet-backdrop" onPointerDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="hp-sheet" role="dialog" aria-label={tile.name}>
        <div className="hp-sheet-head">
          {onBack && <button type="button" className="hp-sheet-back" onClick={onBack}>‹</button>}
          <h3>{tile.name}</h3>
          <button
            type="button"
            className={`hp-power ${isOn ? 'on' : ''}`}
            disabled={!tile.available}
            onClick={() => commit(isOn ? 'turn_off' : 'turn_on', {})}
          >{isOn ? 'On' : 'Off'}</button>
          <button type="button" className="hp-sheet-close" onClick={onClose} aria-label="Close">×</button>
        </div>

        <div className="hp-sheet-body">
          {!tile.available && <p className="hp-note">This light is offline. Check its power or Wi-Fi.</p>}

          {tile.dimmable && tile.available && (
            <Slider
              label="Brightness" min={1} max={100} value={isOn ? (tile.brightness_pct ?? 100) : 0}
              className="hp-slider-bri" format={(v) => `${v}%`}
              onInput={(v) => throttled('turn_on', { brightness_pct: v })}
              onCommit={(v) => commit('turn_on', { brightness_pct: v })}
            />
          )}

          {tile.color_temp && tile.available && (
            <Slider
              label="White" min={tile.min_kelvin} max={tile.max_kelvin} step={50}
              value={tile.color_temp_kelvin ?? 3000} className="hp-slider-ct" format={(v) => `${v}K`}
              onInput={(v) => throttled('turn_on', { color_temp_kelvin: v })}
              onCommit={(v) => commit('turn_on', { color_temp_kelvin: v })}
            />
          )}

          {tile.color && tile.available && (
            <>
              <Slider
                label="Colour" min={0} max={359} value={hue} className="hp-slider-hue" format={(v) => `${v}°`}
                onInput={(v) => throttled('turn_on', { hs_color: [v, Math.max(sat, 40)] })}
                onCommit={(v) => commit('turn_on', { hs_color: [v, Math.max(sat, 40)] })}
              />
              <Slider
                label="Saturation" min={0} max={100} value={sat} className="hp-slider-sat" format={(v) => `${v}%`}
                onInput={(v) => throttled('turn_on', { hs_color: [hue, v] })}
                onCommit={(v) => commit('turn_on', { hs_color: [hue, v] })}
              />
            </>
          )}

          {(tile.color || tile.color_temp) && tile.available && (
            <div className="hp-presets">
              {PRESETS.filter((p) => (p.kelvin ? tile.color_temp : tile.color)).map((p) => {
                const rgb = p.kelvin ? kelvinToRgb(p.kelvin) : null
                const bg = rgb ? `rgb(${rgb.join(',')})` : `hsl(${p.hs[0]} ${p.hs[1]}% 55%)`
                return (
                  <button
                    key={p.label} type="button" className="hp-preset" style={{ background: bg }}
                    onClick={() => commit('turn_on', p.kelvin ? { color_temp_kelvin: p.kelvin } : { hs_color: p.hs })}
                  >{p.label}</button>
                )
              })}
            </div>
          )}

          {tile.effects?.length > 0 && tile.available && (
            <div className="hp-effects">
              {tile.effects.slice(0, 12).map((fx) => (
                <button
                  key={fx} type="button" className={`hp-chip ${tile.effect === fx ? 'active' : ''}`}
                  onClick={() => commit('turn_on', { effect: fx })}
                >{fx}</button>
              ))}
            </div>
          )}

          {members.length > 0 && (
            <div className="hp-members">
              <h4>Bulbs</h4>
              <div className="hp-member-grid">
                {members.map((m) => (
                  <Tile key={m.entity_id} tile={m} onTap={() => openOther(m.entity_id)} onHold={() => openOther(m.entity_id)} />
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export function InfoStrip({ info, now, panelShown, onToggle }) {
  const w = info?.weather
  const routes = info?.traffic || []
  const next = info?.next
  // Formatted in the house's zone, not the terminal's: a Pi left on UTC/en-GB would
  // otherwise show 22:27 at 6:27 PM.
  const timeZone = info?.timezone || undefined
  let time, date
  try {
    time = now.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone })
    date = now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone })
  } catch {
    time = now.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
    date = now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })
  }
  return (
    <div className="hp-strip">
      <div className="hp-strip-cell hp-clock">
        <span className="hp-big">{time}</span>
        <span className="hp-small">{date}</span>
      </div>
      <div className="hp-strip-cell">
        {w ? (
          <>
            <span className="hp-big"><Icon name={WEATHER_ICON[w.condition] || 'cloud'} /> {Math.round(w.temperature)}°</span>
            <span className="hp-small">
              {WEATHER_LABEL[w.condition] || w.condition}
              {w.high != null && ` · ${Math.round(w.high)}°/${w.low != null ? Math.round(w.low) : '–'}°`}
              {w.precip ? ` · ${w.precip}% rain` : ''}
            </span>
          </>
        ) : <span className="hp-small">Weather unavailable</span>}
      </div>
      <div className="hp-strip-cell">
        {routes.length ? routes.slice(0, 2).map((r) => (
          <span key={r.name} className="hp-route">
            <span className="hp-big"><Icon name="car" /> {r.minutes != null ? `${r.minutes} min` : '––'}</span>
            <span className="hp-small">{r.name}{r.minutes == null ? ' · unavailable' : ''}</span>
          </span>
        )) : <span className="hp-small">No drive times</span>}
      </div>
      <div className="hp-strip-cell hp-next">
        {next ? (
          <>
            <span className="hp-big hp-ellipsis">{next.title}</span>
            <span className="hp-small">
              {next.day}{next.time ? ` · ${next.time}` : ''}{next.detail ? ` · ${next.detail}` : ''}
            </span>
          </>
        ) : <span className="hp-small">Nothing scheduled</span>}
      </div>
      <button type="button" className={`hp-hide ${panelShown ? 'active' : ''}`} onClick={onToggle}
        aria-label={panelShown ? 'Hide home controls' : 'Show home controls'}><Icon name="bulb" /></button>
    </div>
  )
}

export default function HomePanel({ deviceId, deviceKey }) {
  const [data, setData] = useState({ rooms: [], scenes: [] })
  const [error, setError] = useState(null)
  const [room, setRoom] = useState(() => {
    try { return localStorage.getItem(`hp-room-${deviceId}`) } catch { return null }
  })
  const [sheet, setSheet] = useState(null) // { id, back }
  const [optimistic, setOptimistic] = useState({})
  const [toast, setToast] = useState(null)
  const auth = `key=${encodeURIComponent(deviceKey)}`

  const loadRooms = useCallback(async () => {
    try {
      const res = await fetch(`/api/devices/home/rooms?${auth}`)
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
      setData(await res.json())
      setError(null)
    } catch (e) {
      setError(String(e.message || e))
    }
  }, [auth])

  useEffect(() => {
    let cancelled = false
    let timer
    const tick = async () => {
      if (document.visibilityState !== 'hidden') await loadRooms()
      if (!cancelled) timer = setTimeout(tick, ROOMS_POLL_MS)
    }
    tick()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [loadRooms])

  // Merge optimistic patches over the polled data until they expire.
  const merged = useMemo(() => {
    const now = Date.now()
    const patch = (t) => {
      const o = optimistic[t.entity_id]
      return o && o.until > now ? { ...t, ...o.patch } : t
    }
    return {
      rooms: data.rooms.map((r) => ({ ...r, tiles: r.tiles.map(patch) })),
      scenes: data.scenes,
    }
  }, [data, optimistic])

  const index = useMemo(() => {
    const out = {}
    merged.rooms.forEach((r) => r.tiles.forEach((t) => { out[t.entity_id] = t }))
    return out
  }, [merged])

  const roomNames = merged.rooms.map((r) => r.name)
  // Last room picked on this terminal, else its home room (?room= in the kiosk URL, or the
  // room the server matched from this terminal's own camera), else
  // wherever the most is switched on -- alphabetical would open every screen on "Bathroom".
  const homeRoom = new URLSearchParams(window.location.search).get('room') || data.home_room
  const busiest = [...merged.rooms].sort((a, b) => b.on - a.on)[0]?.name
  const current = room === SCENES ? SCENES
    : roomNames.includes(room) ? room
      : roomNames.includes(homeRoom) ? homeRoom
        : busiest
  const currentRoom = merged.rooms.find((r) => r.name === current)

  const pickRoom = (name) => {
    setRoom(name)
    try { localStorage.setItem(`hp-room-${deviceId}`, name) } catch { /* kiosk storage can be off */ }
  }

  const flash = (msg) => { setToast(msg); setTimeout(() => setToast(null), 3500) }

  const send = useCallback(async (entityId, service, data = {}) => {
    const patch = {}
    if (service === 'turn_off') patch.state = 'off'
    if (service === 'turn_on') {
      patch.state = 'on'
      if (data.brightness_pct != null) patch.brightness_pct = data.brightness_pct
      if (data.color_temp_kelvin) { patch.color_temp_kelvin = data.color_temp_kelvin; patch.color_mode = 'color_temp' }
      if (data.hs_color) { patch.hs_color = data.hs_color; patch.color_mode = 'hs'; patch.rgb_color = null }
    }
    if (service === 'toggle') {
      const t = index[entityId]
      if (t) patch.state = t.state === 'on' ? 'off' : 'on'
    }
    if (Object.keys(patch).length) {
      setOptimistic((o) => ({ ...o, [entityId]: { until: Date.now() + OPTIMISTIC_MS, patch: { ...(o[entityId]?.patch || {}), ...patch } } }))
    }
    try {
      const res = await fetch(`/api/devices/home/control?${auth}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: deviceId, entity_id: entityId, service, data }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
    } catch (e) {
      setOptimistic((o) => { const n = { ...o }; delete n[entityId]; return n })
      flash(`Couldn't change that: ${e.message || e}`)
    }
    setTimeout(loadRooms, 600)
  }, [auth, deviceId, index, loadRooms])

  const roomOff = async (name) => {
    try {
      const res = await fetch(`/api/devices/home/control?${auth}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: deviceId, room_off: name }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
    } catch (e) {
      flash(`Couldn't turn the room off: ${e.message || e}`)
    }
    loadRooms()
  }

  const tap = (t) => {
    if (!t.available) { flash(`${t.name} is offline`); return }
    if (t.domain === 'scene') send(t.entity_id, 'turn_on')
    else if (t.domain === 'media_player') send(t.entity_id, 'media_play_pause')
    else send(t.entity_id, 'toggle')
  }
  const hold = (t) => {
    if (t.domain === 'light') setSheet({ id: t.entity_id, back: null })
    else tap(t)
  }

  const sheetTile = sheet && index[sheet.id]

  return (
    <div className="hp-panel">
      <div className="hp-rooms" role="tablist">
        {merged.rooms.map((r) => (
          <button
            key={r.name} type="button" role="tab"
            className={`hp-room-tab ${current === r.name ? 'active' : ''}`}
            onClick={() => pickRoom(r.name)}
          >
            {r.name}{r.on > 0 && <span className="hp-room-count">{r.on}</span>}
          </button>
        ))}
        {merged.scenes.length > 0 && (
          <button
            type="button" role="tab"
            className={`hp-room-tab ${current === SCENES ? 'active' : ''}`}
            onClick={() => pickRoom(SCENES)}
          >Scenes</button>
        )}
      </div>

      {error && <div className="hp-error">Home Assistant: {error}</div>}

      <div className="hp-grid">
        {current === SCENES
          ? merged.scenes.map((s) => <Tile key={s.entity_id} tile={s} onTap={() => tap(s)} onHold={() => tap(s)} />)
          : currentRoom?.tiles.map((t) => (
            <Tile key={t.entity_id} tile={t} onTap={() => tap(t)} onHold={() => hold(t)} />
          ))}
      </div>

      {currentRoom && currentRoom.on > 0 && (
        <button type="button" className="hp-room-off" onClick={() => roomOff(currentRoom.name)}>
          Turn off everything in {currentRoom.name}
        </button>
      )}

      {sheetTile && (
        <LightSheet
          key={sheetTile.entity_id}
          tile={sheetTile}
          index={index}
          send={send}
          openOther={(id) => setSheet({ id, back: sheet.id })}
          onBack={sheet.back ? () => setSheet({ id: sheet.back, back: null }) : null}
          onClose={() => setSheet(null)}
        />
      )}

      {toast && <div className="hp-toast">{toast}</div>}
    </div>
  )
}

export function useHomeInfo(deviceKey, enabled) {
  const [info, setInfo] = useState(null)
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    if (!enabled) return undefined
    let cancelled = false
    let timer
    const tick = async () => {
      try {
        const res = await fetch(`/api/devices/home/info?key=${encodeURIComponent(deviceKey)}`)
        if (res.ok && !cancelled) setInfo(await res.json())
      } catch { /* next tick */ }
      if (!cancelled) timer = setTimeout(tick, INFO_POLL_MS)
    }
    tick()
    const clock = setInterval(() => setNow(new Date()), 15000)
    return () => { cancelled = true; clearTimeout(timer); clearInterval(clock) }
  }, [deviceKey, enabled])
  return { info, now }
}
