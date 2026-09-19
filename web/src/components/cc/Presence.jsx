import { TINT } from '../../lib/cc'
import Panel, { PanelEmpty, PanelError } from './Panel'

/**
 * Presence & sensors — where Jack is, what each room is doing, and what the range is up
 * to.
 *
 * The important design rule here is honesty about provenance. Home/away is a real
 * reading from Home Assistant's person entity. The ROOM is an inference (see
 * core/home_state.py): it comes from a motion sensor where one exists, and otherwise
 * from something being switched on in that room. The tile says which, every time, with
 * a dotted underline on an inferred answer. Once the house has real per-room occupancy
 * sensors, the same tile starts reading "motion" instead and nothing else changes.
 *
 * Tiles with no sensor behind them at all render '—' rather than a plausible-looking
 * number. A dashboard that invents a reading is worse than one that admits a gap.
 */

function Tile({ label, value, sub, tint, faded }) {
  return (
    <div className={`cc-sensor ${faded ? 'is-faded' : ''}`}>
      <span className="cc-sensor-label">{label}</span>
      <span className="cc-sensor-value" style={tint ? { color: tint } : undefined}>{value}</span>
      {sub && <span className="cc-sensor-sub">{sub}</span>}
    </div>
  )
}

function rangeTint(status) {
  if (status === 'cooking') return TINT.warn
  if (status === 'offline') return TINT.idle
  return TINT.bright
}

export default function Presence({ home, onOpen, onOpenCameras }) {
  if (!home) {
    return (
      <Panel title="Presence & sensors" onOpen={onOpen}>
        <PanelEmpty>Reading the house…</PanelEmpty>
      </Panel>
    )
  }

  const { presence, rooms = [], range, cameras } = home

  if (!home.available) {
    return (
      <Panel title="Presence & sensors" meta="offline" metaTint={TINT.warn} onOpen={onOpen}>
        <PanelError>{home.error}</PanelError>
        <div className="cc-sensor-grid">
          <Tile label="Cameras" value={cameras?.enabled ?? '—'}
                sub={cameras?.total ? `of ${cameras.total} registered` : 'none registered'} />
        </div>
      </Panel>
    )
  }

  const inferred = presence?.source === 'activity'
  const presenceTint = presence?.home === false ? TINT.warn
    : presence?.home ? TINT.bright : TINT.idle

  // Rooms with a real reading come first; the rest still render, greyed, so the panel
  // shows the shape of the house rather than only its instrumented corners.
  const sensed = rooms.filter((r) => r.temp_f != null || r.motion_live || r.activity_live)
  const quiet = rooms.filter((r) => !sensed.includes(r))

  return (
    <Panel
      title="Presence & sensors"
      meta={`${cameras?.enabled ?? 0} cameras`}
      metaTint={TINT.dim}
      onOpen={onOpen}
    >
      <div className="cc-presence">
        <span className="cc-presence-dot" style={{ background: presenceTint }} />
        <div className="cc-presence-body">
          <span className="cc-presence-where" style={{ color: presenceTint }}>
            {presence?.label || 'Unknown'}
          </span>
          <span className={`cc-presence-how ${inferred ? 'is-inferred' : ''}`}>
            {presence?.detail}
          </span>
        </div>
      </div>

      <div className="cc-sensor-grid">
        {sensed.map((room) => (
          <Tile
            key={room.key}
            label={room.name}
            value={room.temp_f != null ? `${room.temp_f}°F` : (room.motion_live ? 'Motion' : 'Active')}
            sub={room.motion_live ? 'motion now'
              : room.activity_live ? (room.activity_by || 'something on').toLowerCase()
                : room.camera ? 'camera only' : ''}
            tint={room.motion_live ? TINT.ok : undefined}
          />
        ))}

        <Tile
          label="Range"
          value={range?.available ? range.headline : '—'}
          sub={range?.available
            ? [range.door_open ? 'door open' : null, range.oven_mode && range.oven_mode !== 'others' ? range.oven_mode : null]
              .filter(Boolean).join(' · ') || range.status
            : 'not reporting'}
          tint={range?.available ? rangeTint(range.status) : undefined}
          faded={!range?.available}
        />

        <button type="button" className="cc-sensor is-button" onClick={(e) => { e.stopPropagation(); onOpenCameras?.() }}>
          <span className="cc-sensor-label">Cameras</span>
          <span className="cc-sensor-value">{cameras?.enabled ?? 0} live</span>
          <span className="cc-sensor-sub">tap to watch</span>
        </button>

        {quiet.map((room) => (
          <Tile key={room.key} label={room.name} value="—"
                sub={room.camera ? 'camera only' : 'no sensor'} faded />
        ))}
      </div>
    </Panel>
  )
}
