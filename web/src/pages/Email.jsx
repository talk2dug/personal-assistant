import { useEffect, useState } from 'react'
import { api } from '../api'
import ConfirmMailActionModal from '../components/email/ConfirmMailActionModal'
import ConfirmSendModal from '../components/email/ConfirmSendModal'
import './email.css'

/** A message's full body is a separate, heavier IMAP fetch than the list -- lazily
 *  loaded the first time a row is opened, same idiom as DisputeItemRow's letters.
 *  onChanged is called after mark-read/archive/delete actually go through, so the
 *  inbox list behind this detail view picks up the change too. */
function MessageDetail({ uid, folder, onClose, onChanged }) {
  const [message, setMessage] = useState(null)
  const [error, setError] = useState(null)
  const [pendingAction, setPendingAction] = useState(null)
  const [marking, setMarking] = useState(false)

  useEffect(() => {
    setMessage(null)
    setError(null)
    api.readEmail(uid, folder).then(setMessage).catch((e) => setError(e.message))
  }, [uid, folder])

  async function handleMarkRead() {
    setMarking(true)
    try {
      await api.markEmailRead(uid, folder)
      onChanged?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setMarking(false)
    }
  }

  function afterAction() {
    setPendingAction(null)
    onChanged?.()
    onClose()
  }

  return (
    <div className="email-detail">
      <div className="email-detail-header">
        <h4>{message ? message.subject || '(no subject)' : 'Loading…'}</h4>
        <div className="email-detail-actions">
          <button className="task-drop" onClick={handleMarkRead} disabled={marking || !message} title="Mark read">
            ✓
          </button>
          <button className="task-drop" onClick={() => setPendingAction('archive')} disabled={!message} title="Archive">
            🗄
          </button>
          <button className="task-drop" onClick={() => setPendingAction('delete')} disabled={!message} title="Delete">
            🗑
          </button>
          <button className="task-drop" onClick={onClose} title="Close">✕</button>
        </div>
      </div>
      {error && <p className="empty-hint">Couldn't load that message ({error}).</p>}
      {message && (
        <>
          <div className="email-detail-meta">
            <div><span className="hud-label">From</span> {message.from}</div>
            <div><span className="hud-label">Date</span> {message.date}</div>
          </div>
          <pre className="email-detail-body">{message.body}</pre>
        </>
      )}
      {pendingAction && message && (
        <ConfirmMailActionModal
          action={pendingAction}
          message={{ uid, folder, subject: message.subject, from: message.from }}
          onClose={() => setPendingAction(null)}
          onDone={afterAction}
        />
      )}
    </div>
  )
}

function InboxPanel() {
  const [messages, setMessages] = useState(null)
  const [query, setQuery] = useState('')
  const [openUid, setOpenUid] = useState(null)
  const [error, setError] = useState(null)
  const [pendingAction, setPendingAction] = useState(null)

  async function load() {
    try {
      const result = await api.listEmails('INBOX', 20, query || undefined)
      setMessages(result.emails || [])
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => { load() }, [query])

  async function handleMarkRead(m, e) {
    e.stopPropagation()
    try {
      await api.markEmailRead(m.uid, 'INBOX')
      setMessages((prev) => prev.map((x) => (x.uid === m.uid ? { ...x, unread: false } : x)))
    } catch (err) {
      setError(err.message)
    }
  }

  function requestAction(action, m, e) {
    e.stopPropagation()
    setPendingAction({ action, message: { uid: m.uid, folder: 'INBOX', subject: m.subject, from: m.from } })
  }

  function afterAction() {
    setPendingAction(null)
    if (openUid === pendingAction?.message.uid) setOpenUid(null)
    load()
  }

  if (messages === null) return <p className="empty-hint">Loading…</p>

  return (
    <section>
      <div className="tasks-header">
        <h3>Inbox</h3>
        <input
          className="email-search"
          placeholder="Search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {error && <p className="empty-hint">{error}</p>}
      {messages.length === 0 && <p className="empty-hint">Nothing here.</p>}
      <ul className="task-list">
        {messages.map((m) => (
          <li
            key={m.uid}
            className={`task-row email-row ${m.unread ? 'is-unread' : ''}`}
            onClick={() => setOpenUid(m.uid)}
          >
            <div className="task-body">
              <div className="task-text">{m.subject || '(no subject)'}</div>
              <div className="task-meta">
                <span>{m.from}</span>
                <span>{m.date}</span>
              </div>
            </div>
            <div className="email-row-actions">
              {m.unread && (
                <button className="task-drop" onClick={(e) => handleMarkRead(m, e)} title="Mark read">✓</button>
              )}
              <button className="task-drop" onClick={(e) => requestAction('archive', m, e)} title="Archive">🗄</button>
              <button className="task-drop" onClick={(e) => requestAction('delete', m, e)} title="Delete">🗑</button>
            </div>
          </li>
        ))}
      </ul>
      {openUid && (
        <MessageDetail uid={openUid} folder="INBOX" onClose={() => setOpenUid(null)} onChanged={load} />
      )}
      {pendingAction && (
        <ConfirmMailActionModal
          action={pendingAction.action}
          message={pendingAction.message}
          onClose={() => setPendingAction(null)}
          onDone={afterAction}
        />
      )}
    </section>
  )
}

/** Phase 3 visibility: the autonomous junk-scan (scheduler.run_mail_junk_scan) has always
 *  run unattended, moving scored-junk mail into Junk every mail_junk_scan_interval_seconds
 *  with no confirmation -- correct as-is, not something this page changes. This just reads
 *  back the audit trail it now leaves (mail_junk_log) so that isn't invisible beyond a
 *  server log line. Nothing here is actionable; there's no undo button because the
 *  underlying move already happened autonomously, on purpose, before this page ever
 *  loads. */
function JunkLogPanel() {
  const [entries, setEntries] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.mailJunkLog(50).then((r) => setEntries(r.entries || [])).catch((e) => setError(e.message))
  }, [])

  if (entries === null) return <p className="empty-hint">Loading…</p>

  return (
    <section>
      <h3>Auto-junked</h3>
      <p className="empty-hint">
        The background junk scan moves anything it scores as junk straight into the Junk
        folder on its own, no confirmation needed. This is the record of what it's done.
      </p>
      {error && <p className="empty-hint">{error}</p>}
      {entries.length === 0 && <p className="empty-hint">Nothing auto-junked yet.</p>}
      <ul className="task-list">
        {entries.map((entry) => (
          <li key={entry.id} className="task-row email-row">
            <div className="task-body">
              <div className="task-text">{entry.subject || '(no subject)'}</div>
              <div className="task-meta">
                <span>{entry.from_address}</span>
                <span>score {entry.score}</span>
                <span>{entry.moved ? `moved to ${entry.moved_to || 'Junk'}` : 'flagged, move failed'}</span>
                <span>{entry.created_at}</span>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** Bills the background scan (mail_bills.py) found in incoming mail. Read-only on
 *  purpose: this pass only ever reads and records, and the place to actually act on a
 *  bill is the Review queue, where confirming/dismissing writes back to the row. Amounts
 *  and dates are shown exactly as the message worded them when they weren't clean enough
 *  to parse ("$80-$120", "due on receipt") rather than being rounded into a number this
 *  page can't stand behind. */
function BillsPanel() {
  const [bills, setBills] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.mailBills(50).then((r) => setBills(r.bills || [])).catch((e) => setError(e.message))
  }, [])

  if (bills === null) return <p className="empty-hint">Loading…</p>

  return (
    <section>
      <h3>Bills</h3>
      <p className="empty-hint">
        Bills spotted in incoming mail, with a reminder set a few days ahead of any real
        due date. Nothing here is paid, filed, or added to your recurring charges — confirm
        or dismiss them in the Review queue.
      </p>
      {error && <p className="empty-hint">{error}</p>}
      {bills.length === 0 && <p className="empty-hint">No bills detected yet.</p>}
      <ul className="task-list">
        {bills.map((bill) => (
          <li key={bill.id} className="task-row email-row">
            <div className="task-body">
              <div className="task-text">
                {bill.payee || bill.subject || '(unknown payee)'}
                {bill.amount_text ? ` — ${bill.amount_text}` : ''}
              </div>
              <div className="task-meta">
                <span>{bill.due_date ? `due ${bill.due_date}` : bill.due_date_text || 'no due date'}</span>
                {bill.is_recurring && <span>recurring{bill.cadence ? ` (${bill.cadence})` : ''}</span>}
                {bill.reminder_id && <span>reminder set</span>}
                <span>{bill.status}</span>
                <span>{bill.from_address}</span>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

const IMPORTANCE_CATEGORY_LABELS = {
  personal_finances: 'personal finances',
  relationships: 'relationships',
  personal_business: 'personal business',
  other: 'other',
}

/** Provisionally-important mail (mail_importance.py) and, more to the point, how well
 *  the flagging is actually tracking the owner's own judgement. The scoreboard is the
 *  reason this tab exists: the feature is a feedback loop, and a loop whose accuracy
 *  nobody can see is one nobody will keep answering. Verdicts are given in the Review
 *  queue (each one becomes a labelled example the next scan reads back), so this view is
 *  read-only — there is nothing to act on here. */
function ImportancePanel() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.mailImportance(50).then(setData).catch((e) => setError(e.message))
  }, [])

  if (data === null) return <p className="empty-hint">{error || 'Loading…'}</p>

  const flags = data.flags || []
  const stats = data.stats || {}
  // null precision means he hasn't ruled on anything yet — rendering that as 0% would
  // read as "this is always wrong" on day one, which isn't what no data means.
  const precision = stats.precision === null || stats.precision === undefined
    ? null : Math.round(stats.precision * 100)

  return (
    <section>
      <h3>Important</h3>
      <p className="empty-hint">
        Mail Jarvis tentatively thinks mattered to you — finances, relationships, personal
        business. Each one asks you in the Review queue whether it really was; your answer
        (and anything you type with it) is what calibrates the next scan. Nothing here is
        read, moved or replied to.
      </p>
      <p className="empty-hint">
        {stats.flagged_total || 0} flagged from {stats.messages_judged || 0} judged ·
        {' '}you confirmed {stats.confirmed || 0}, rejected {stats.rejected || 0}
        {precision === null ? ' · no verdicts yet' : ` · ${precision}% of flags confirmed`}
        {stats.awaiting_verdict ? ` · ${stats.awaiting_verdict} awaiting your verdict` : ''}
        {' '}· learning from {stats.examples_total || 0} of your rulings
        {' '}({stats.examples_positive || 0} important / {stats.examples_negative || 0} not)
      </p>
      {error && <p className="empty-hint">{error}</p>}
      {flags.length === 0 && <p className="empty-hint">Nothing flagged as important yet.</p>}
      <ul className="task-list">
        {flags.map((flag) => (
          <li key={flag.id} className="task-row email-row">
            <div className="task-body">
              <div className="task-text">{flag.subject || '(no subject)'}</div>
              <div className="task-meta">
                <span>{IMPORTANCE_CATEGORY_LABELS[flag.category] || flag.category}</span>
                {flag.confidence !== null && flag.confidence !== undefined && (
                  <span>confidence {Number(flag.confidence).toFixed(2)}</span>
                )}
                <span>
                  {flag.status === 'confirmed' && 'you confirmed it was important'}
                  {flag.status === 'rejected' && "you said it wasn't"}
                  {flag.status === 'flagged' && 'awaiting your verdict'}
                </span>
                <span>{flag.from_address}</span>
              </div>
              {flag.reason && <div className="task-meta"><span>{flag.reason}</span></div>}
              {flag.verdict_note && (
                <div className="task-meta"><span>You said: {flag.verdict_note}</span></div>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

function ComposePanel() {
  const [to, setTo] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [pending, setPending] = useState(null)
  const [sent, setSent] = useState(false)

  function review(e) {
    e.preventDefault()
    if (!to.trim() || !subject.trim() || !body.trim()) return
    setSent(false)
    setPending({ to: to.trim(), subject: subject.trim(), body })
  }

  function afterSent() {
    setPending(null)
    setSent(true)
    setTo('')
    setSubject('')
    setBody('')
  }

  return (
    <section>
      <h3>Compose</h3>
      <form className="task-form email-compose-form" onSubmit={review}>
        <input placeholder="To" type="email" value={to} onChange={(e) => setTo(e.target.value)} />
        <input placeholder="Subject" value={subject} onChange={(e) => setSubject(e.target.value)} />
        <textarea
          className="email-compose-body"
          placeholder="Write your message…"
          rows={10}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
        <div className="task-form-actions">
          <button type="submit">Review &amp; send…</button>
        </div>
      </form>
      {sent && <p className="empty-hint">Sent.</p>}
      {pending && (
        <ConfirmSendModal draft={pending} onClose={() => setPending(null)} onSent={afterSent} />
      )}
    </section>
  )
}

export default function Email() {
  const [tab, setTab] = useState('inbox')

  return (
    <div className="email-page">
      <div className="kitchen-tabs">
        <button className={`kitchen-tab ${tab === 'inbox' ? 'active' : ''}`} onClick={() => setTab('inbox')}>
          Inbox
        </button>
        <button className={`kitchen-tab ${tab === 'compose' ? 'active' : ''}`} onClick={() => setTab('compose')}>
          Compose
        </button>
        <button className={`kitchen-tab ${tab === 'bills' ? 'active' : ''}`} onClick={() => setTab('bills')}>
          Bills
        </button>
        <button
          className={`kitchen-tab ${tab === 'important' ? 'active' : ''}`}
          onClick={() => setTab('important')}
        >
          Important
        </button>
        <button className={`kitchen-tab ${tab === 'junk' ? 'active' : ''}`} onClick={() => setTab('junk')}>
          Auto-junked
        </button>
      </div>
      {tab === 'inbox' && <InboxPanel />}
      {tab === 'compose' && <ComposePanel />}
      {tab === 'bills' && <BillsPanel />}
      {tab === 'important' && <ImportancePanel />}
      {tab === 'junk' && <JunkLogPanel />}
    </div>
  )
}
