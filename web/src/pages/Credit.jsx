import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import ScoreEntryForm from '../components/credit/ScoreEntryForm'
import ScoreHistoryChart from '../components/credit/ScoreHistoryChart'
import ScoreHistoryTable from '../components/credit/ScoreHistoryTable'
import DisputeCreateForm from '../components/credit/DisputeCreateForm'
import DisputeItemRow from '../components/credit/DisputeItemRow'
import './credit.css'

const STATUS_FILTERS = [
  { value: '', label: 'All' },
  { value: 'drafted', label: 'Drafted' },
  { value: 'mailed', label: 'Mailed' },
  { value: 'resolved', label: 'Resolved' },
]

/**
 * Credit score history (manual entry, no live bureau feed) + the credit-report dispute
 * tracker. The dispute tracker's only sensitive action -- actually spending postage via
 * LetterStream -- lives entirely inside ConfirmMailModal (see DisputeItemRow), which is
 * the one place in this whole page that can trigger POST /letters/:id/mail.
 */
export default function Credit() {
  const [scores, setScores] = useState(null)
  const [disputes, setDisputes] = useState(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [error, setError] = useState(null)

  const loadScores = useCallback(async () => {
    try {
      setScores(await api.creditScores())
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [])

  const loadDisputes = useCallback(async () => {
    try {
      setDisputes(await api.creditDisputes({ status: statusFilter || undefined }))
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [statusFilter])

  useEffect(() => { loadScores() }, [loadScores])
  useEffect(() => { loadDisputes() }, [loadDisputes])

  return (
    <div className="credit-page">
      <h2>Credit</h2>
      {error && <p className="credit-form-error">{error}</p>}

      <section>
        <h3>Score history</h3>
        {scores === null ? (
          <p className="empty-hint">Loadingâ€¦</p>
        ) : (
          <>
            <ScoreHistoryChart entries={scores} />
            <ScoreEntryForm onAdded={loadScores} />
            <ScoreHistoryTable entries={scores} onChange={loadScores} />
          </>
        )}
      </section>

      <section>
        <div className="dispute-tracker-header">
          <h3>Dispute tracker</h3>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            {STATUS_FILTERS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </div>
        <DisputeCreateForm onCreated={loadDisputes} />
        {disputes === null ? (
          <p className="empty-hint">Loadingâ€¦</p>
        ) : disputes.length === 0 ? (
          <p className="empty-hint">No disputes tracked yet.</p>
        ) : (
          <div className="dispute-list">
            {disputes.map((item) => (
              <DisputeItemRow key={item.id} item={item} onChange={loadDisputes} />
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
