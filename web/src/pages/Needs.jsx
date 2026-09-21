import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import './needs.css'

/**
 * The project board — per pipeline, what the team needs from him and what it is working on.
 *
 * Two lanes, blockers first. Business tasks previously existed only as two uncounted
 * numbers on the Command Center ("Backlog", "Building") with nothing to click, while the
 * section actually labelled "Tasks" showed personal_tasks — a different table entirely.
 * So the team's own plan was invisible to the person it was being run for.
 *
 * The Review queue answers "which of these do you prefer"; this answers "we cannot go
 * any further without something only you can get". They are kept apart deliberately:
 * a handful of items that halt a whole pipeline must not be buried under a stream of
 * mockups to approve.
 *
 * Everything is answerable in place. The point of the board is that supplying the thing
 * is the same gesture as reading about it — paste the token into the row that asked for
 * it and the agent that was blocked picks it up on its next run. Sending him elsewhere
 * to go paste a key into a config file is how the last store ended up with two empty
 * strings and five weeks of zero sales.
 */

const KIND_LABEL = {
  secret: 'API key / token',
  url: 'Link',
  text: 'Value',
  file: 'File to import',
  account: 'Account to create',
  purchase: 'Purchase',
}

// What the input should look like for each kind. A secret gets a password field so it
// isn't left legible on screen; a file/account ask is answered with a path or a note.
const PLACEHOLDER = {
  secret: 'Paste the key — stored, never shown back',
  url: 'https://…',
  text: 'The value they asked for',
  file: 'Full path to the file or folder',
  account: 'Account name / id once created',
  purchase: 'What you bought, or the account it is on',
}

function Prompts({ prompts }) {
  const [copied, setCopied] = useState(-1)
  if (!prompts || !prompts.length) return null

  async function copy(text, i) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(i)
      setTimeout(() => setCopied(-1), 1400)
    } catch {
      setCopied(-1)                       // clipboard blocked; the text is still selectable
    }
  }

  return (
    <div className="needs-prompts">
      <div className="needs-prompts-head">
        Paste into Leonardo — {prompts.length} prompt{prompts.length === 1 ? '' : 's'}
      </div>
      {prompts.map((p, i) => (
        <div className="needs-prompt" key={i}>
          <code>{p}</code>
          <button type="button" className="needs-copy" onClick={() => copy(p, i)}>
            {copied === i ? 'COPIED' : 'COPY'}
          </button>
        </div>
      ))}
    </div>
  )
}

const TASK_NEXT = { open: 'doing', doing: 'done', done: 'open' }

function Task({ task, onChanged }) {
  const [busy, setBusy] = useState(false)

  async function advance(status) {
    if (busy) return
    setBusy(true)
    try {
      await api.setBusinessTaskStatus(task.id, status)
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className={`needs-task t-${task.status}`}>
      <button
        type="button"
        className="needs-task-tick"
        title={`Mark ${TASK_NEXT[task.status] || 'done'}`}
        disabled={busy}
        onClick={() => advance(TASK_NEXT[task.status] || 'done')}
      >
        {task.status === 'done' ? '✓' : task.status === 'doing' ? '●' : '○'}
      </button>
      <span className="needs-task-text">{task.text}</span>
      {task.priority === 'high' && <span className="needs-task-pri">HIGH</span>}
      {task.status !== 'done' && (
        <button
          type="button"
          className="needs-task-drop"
          disabled={busy}
          onClick={() => advance('dropped')}
        >
          DROP
        </button>
      )}
    </li>
  )
}

function Row({ item, onAnswered }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const answered = item.status === 'provided' || item.status === 'resolved'

  async function submit(e) {
    e.preventDefault()
    if (!value.trim() || busy) return
    setBusy(true); setError(null)
    try {
      await api.provideNeed(item.id, value.trim())
      setValue('')
      onAnswered()
    } catch (err) {
      setError(err?.message || 'could not save that')
    } finally {
      setBusy(false)
    }
  }

  async function decline() {
    setBusy(true); setError(null)
    try {
      await api.setNeedStatus(item.id, 'rejected', 'declined from the board')
      onAnswered()
    } catch (err) {
      setError(err?.message || 'could not update that')
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className={`needs-row is-${item.status} p${item.priority}`}>
      <div className="needs-row-head">
        <span className="needs-kind">{KIND_LABEL[item.kind] || item.kind}</span>
        <span className="needs-title">{item.title}</span>
        {item.agent_key && <span className="needs-who">{item.agent_key.replace(/_/g, ' ')}</span>}
        <span className={`needs-status s-${item.status}`}>{item.status}</span>
      </div>

      {item.why && <p className="needs-why">{item.why}</p>}
      {item.blocks && (
        <p className="needs-blocks"><span>BLOCKS</span> {item.blocks}</p>
      )}
      {item.instructions && (
        <details className="needs-how">
          <summary>How to get it</summary>
          <p>{item.instructions}</p>
        </details>
      )}

      <Prompts prompts={item.prompts} />

      {answered ? (
        <div className="needs-answered">
          {item.is_secret ? 'Stored' : 'Provided'}
          {item.value ? <code>{item.value}</code> : null}
          {item.status === 'provided' && <em>waiting for the team to confirm it works</em>}
        </div>
      ) : item.status === 'rejected' ? (
        <div className="needs-answered">Declined{item.note ? ` — ${item.note}` : ''}</div>
      ) : (
        <form className="needs-give" onSubmit={submit}>
          <input
            type={item.kind === 'secret' ? 'password' : 'text'}
            value={value}
            autoComplete="off"
            placeholder={PLACEHOLDER[item.kind] || 'Their answer'}
            onChange={(e) => setValue(e.target.value)}
          />
          <button type="submit" className="needs-send" disabled={busy || !value.trim()}>
            {busy ? '…' : 'GIVE'}
          </button>
          <button type="button" className="needs-decline" onClick={decline} disabled={busy}>
            NOT DOING
          </button>
        </form>
      )}
      {error && <p className="needs-error">{error}</p>}
    </li>
  )
}

export default function Needs() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      setData(await api.needs())
      setError(null)
    } catch (err) {
      setError(err?.message || 'could not load the board')
    }
  }, [])

  useEffect(() => { load() }, [load])

  if (error) return <p className="needs-error">{error}</p>
  if (!data) return <p className="needs-empty">Loading…</p>

  const { summary, groups } = data
  const anythingOpen = summary.open > 0

  return (
    <div className="needs">
      <div className="needs-summary">
        {anythingOpen ? (
          <>
            <strong>{summary.open}</strong> waiting on you
            {summary.top?.blocks && <span className="needs-top"> — top one blocks {summary.top.blocks}</span>}
          </>
        ) : (
          <>Nothing is waiting on you. {summary.provided > 0
            && <span className="needs-top">{summary.provided} answer{summary.provided === 1 ? '' : 's'} not yet confirmed by the team.</span>}</>
        )}
      </div>

      {groups.length === 0 && (
        <p className="needs-empty">
          The team has not asked for anything yet. Requests appear here the moment an
          agent hits something it cannot do without you.
        </p>
      )}

      {groups.map((group) => {
        const counts = group.task_counts || {}
        const live = (counts.doing || 0) + (counts.open || 0)
        const blocked = group.items.filter((i) => i.status === 'open').length
        return (
          <section className="needs-group" key={group.project_id ?? 'none'}>
            <header className="needs-group-head">
              <h3>{group.name}</h3>
              <span className="needs-group-meta">
                {blocked > 0 && <b>{blocked} blocked</b>}
                {live > 0 && <span>{counts.doing || 0} in hand · {counts.open || 0} queued</span>}
                {counts.done > 0 && <span>{counts.done} done</span>}
              </span>
            </header>
            {group.goal && <p className="needs-goal">{group.goal}</p>}

            {group.items.length > 0 && (
              <>
                <h4 className="needs-lane">They need you</h4>
                <ul className="needs-list">
                  {group.items.map((item) => (
                    <Row key={item.id} item={item} onAnswered={load} />
                  ))}
                </ul>
              </>
            )}

            {group.tasks.length > 0 && (
              <>
                <h4 className="needs-lane">They're working on</h4>
                <ul className="needs-tasks">
                  {group.tasks.map((task) => (
                    <Task key={task.id} task={task} onChanged={load} />
                  ))}
                </ul>
              </>
            )}
          </section>
        )
      })}
    </div>
  )
}
