import { useEffect, useRef, useState } from 'react'
import { TINT, hhmm } from '../../lib/cc'
import Panel, { PanelEmpty } from './Panel'

const KIND_TINT = { warn: TINT.warn, dim: TINT.dim, ok: TINT.ok, base: TINT.text }

// Characters per tick of the typing effect, and how often a tick fires.
const TYPE_CHARS = 2
const TYPE_MS = 24

/**
 * The live event log — what Jarvis, his staff and the house have actually done lately,
 * merged from every table that records an action (see _events in command_center.py).
 *
 * The newest line types itself in. This is the one piece of pure theatre on the board
 * and it earns its place: on a screen that mostly sits still, a line arriving character
 * by character is what tells you from across the room that the system is alive and
 * working. Crucially it types REAL text that is already in the feed — it never invents
 * a line to have something to animate, and when nothing new has happened the log simply
 * sits there.
 */
export default function EventLog({ events, onOpen }) {
  const list = events || []
  const newest = list[0]
  const [typed, setTyped] = useState('')
  const typingFor = useRef(null)

  useEffect(() => {
    if (!newest) return
    const key = `${newest.at}|${newest.text}`
    // Only animate a line the first time it appears. Without this, every 5s poll would
    // restart the same line and the log would stutter permanently.
    if (typingFor.current === key) return
    typingFor.current = key
    setTyped('')

    let i = 0
    const id = setInterval(() => {
      i += TYPE_CHARS
      setTyped(newest.text.slice(0, i))
      if (i >= newest.text.length) clearInterval(id)
    }, TYPE_MS)
    return () => clearInterval(id)
  }, [newest])

  const settled = list.slice(1, 9)
  const typing = newest && typed.length < newest.text.length

  return (
    <Panel title="Event log" meta="live" metaTint={TINT.dim} onOpen={onOpen} grow className="cc-log-panel cc-flexi">
      {!events && <PanelEmpty>Loading…</PanelEmpty>}
      {events && list.length === 0 && <PanelEmpty>Nothing logged yet.</PanelEmpty>}

      <div className="cc-scrollbody">
        {newest && (
        <div className="cc-log-row">
          <span className="cc-log-time">{hhmm(newest.at)}</span>
          <span className="cc-log-text" style={{ color: KIND_TINT[newest.kind] || TINT.text }}>
            {typing ? typed : newest.text}
            {typing && <span className="cc-caret">▌</span>}
          </span>
        </div>
      )}

        {settled.map((e, i) => (
          <div className="cc-log-row" key={`${e.at}-${i}`}>
            <span className="cc-log-time">{hhmm(e.at)}</span>
            <span className="cc-log-text" style={{ color: KIND_TINT[e.kind] || TINT.text }}>{e.text}</span>
          </div>
        ))}
      </div>
    </Panel>
  )
}
