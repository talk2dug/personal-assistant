import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * Read-only view of what the vision feature currently knows: registered cameras, who's
 * been seen where recently, and everyone enrolled. Deliberately no "add a person" form
 * here -- enrollment is a Review-page decision (an unfamiliar face seen a few times
 * shows up there with a name field), not something typed in through a dashboard.
 */
export default function Vision() {
  const [cameras, setCameras] = useState([])
  const [people, setPeople] = useState([])
  const [presence, setPresence] = useState({})
  const [error, setError] = useState(null)

  async function load() {
    try {
      const [c, p, pr] = await Promise.all([
        api.visionCameras(), api.visionKnownPeople(), api.visionPresence(),
      ])
      setCameras(c.cameras)
      setPeople(p.people)
      setPresence(pr.presence)
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 10000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="vision-page">
      <header>
        <h1>Vision</h1>
        <p>Cameras, known people, and who the house currently sees.</p>
      </header>

      {error && <div className="review-error">Couldn&rsquo;t load vision data: {error}</div>}

      <section>
        <h2>Cameras</h2>
        {cameras.length === 0 && <p>No cameras registered yet.</p>}
        <ul>
          {cameras.map((c) => (
            <li key={c.key}>
              <strong>{c.name}</strong> ({c.key}) &mdash; {c.location || 'no location set'}
              {!c.enabled && ' — disabled'}
              {c.device_id && ` — linked to device ${c.device_id}`}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2>Right now</h2>
        {Object.keys(presence).length === 0 && <p>Nothing seen recently.</p>}
        <ul>
          {Object.entries(presence).map(([cam, info]) => (
            <li key={cam}>
              <strong>{cam}</strong>: {info.person ? 'person' : 'nobody'}
              {info.pet && ' + pet'}
              {info.people.length > 0 && ` — ${info.people.join(', ')}`}
              {info.unknown_people > 0 && ` — ${info.unknown_people} unrecognised sighting(s)`}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2>Known people</h2>
        {people.length === 0 && (
          <p>
            Nobody enrolled yet &mdash; enrollment happens from the Review page once an
            unfamiliar face has been seen a few times.
          </p>
        )}
        <ul>
          {people.map((p) => (
            <li key={p.key}>
              {p.name} {p.relationship ? `(${p.relationship})` : ''} &mdash; {p.sample_count} sample(s)
              {p.linked_user_id ? ' — linked to a Jarvis account' : ''}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
