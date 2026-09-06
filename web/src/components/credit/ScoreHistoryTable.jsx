import { api } from '../../api'

const BUREAU_LABEL = { experian: 'Experian', equifax: 'Equifax', transunion: 'TransUnion', other: 'Other' }

export default function ScoreHistoryTable({ entries, onChange }) {
  async function remove(id) {
    await api.deleteCreditScore(id)
    onChange()
  }

  if (entries.length === 0) {
    return <p className="empty-hint">No scores recorded yet â€” add one above after you check it somewhere.</p>
  }

  const newestFirst = [...entries].sort(
    (a, b) => (a.recorded_on < b.recorded_on ? 1 : a.recorded_on > b.recorded_on ? -1 : b.id - a.id)
  )

  return (
    <table className="credit-score-table">
      <thead>
        <tr><th>Date</th><th>Bureau</th><th>Score</th><th>Source</th><th>Notes</th><th /></tr>
      </thead>
      <tbody>
        {newestFirst.map((e) => (
          <tr key={e.id}>
            <td>{e.recorded_on}</td>
            <td className={`credit-bureau-${e.bureau}`}>{BUREAU_LABEL[e.bureau] || e.bureau}</td>
            <td>{e.score}</td>
            <td>{e.source || 'â€”'}</td>
            <td>{e.notes || 'â€”'}</td>
            <td><button className="credit-row-remove" onClick={() => remove(e.id)} title="Delete">âœ•</button></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
