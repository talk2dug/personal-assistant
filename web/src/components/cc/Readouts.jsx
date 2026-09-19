import { tintOf } from '../../lib/cc'

/**
 * The strip along the bottom: eight numbers, each answering one question without
 * needing a panel of its own.
 *
 * Each readout knows which section it belongs to, so the whole strip is navigation as
 * well as telemetry — seeing "3 late" and being one click from the thing that is late
 * is the entire point of putting it there.
 */
export default function Readouts({ readouts, onOpen }) {
  const list = readouts || []
  if (!list.length) return null

  // Maps a readout to the section its detail lives in. Anything unmapped simply isn't
  // clickable rather than opening something arbitrary.
  const SECTION = {
    'Credit score': 'credit',
    Inbox: 'email',
    Tasks: 'tasks',
    'Review queue': 'review',
    Pantry: 'kitchen',
    'Meal plan': 'kitchen',
    Shopping: 'grocery',
    Staff: 'agents',
  }

  return (
    <div className="cc-readouts">
      {list.map((r) => {
        const section = SECTION[r.label]
        const Tag = section ? 'button' : 'div'
        return (
          <Tag
            key={r.label}
            className={`cc-readout ${section ? 'is-open' : ''}`}
            type={section ? 'button' : undefined}
            onClick={section ? () => onOpen(section) : undefined}
          >
            <span className="cc-readout-label">{r.label}</span>
            <span className="cc-readout-value">{r.value}</span>
            <span className="cc-readout-sub" style={{ color: tintOf(r.k) }}>{r.sub}</span>
          </Tag>
        )
      })}
    </div>
  )
}
