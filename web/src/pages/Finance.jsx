import { useEffect, useState } from 'react'
import { api } from '../api'
import BalanceCards from '../components/finance/BalanceCards'
import ProjectionChart from '../components/finance/ProjectionChart'
import CalendarGrid from '../components/finance/CalendarGrid'
import GoalsList from '../components/finance/GoalsList'
import SpendingByCategory from '../components/finance/SpendingByCategory'
import RecurringCharges from '../components/finance/RecurringCharges'

export default function Finance() {
  const [summary, setSummary] = useState(null)
  const [projection, setProjection] = useState(null)
  const [loading, setLoading] = useState(true)

  async function loadAll() {
    const [summaryData, projectionData] = await Promise.all([
      api.financeSummary(),
      api.financeProjection(180),
    ])
    setSummary(summaryData)
    setProjection(projectionData)
    setLoading(false)
  }

  useEffect(() => {
    loadAll()
  }, [])

  async function refreshProjection() {
    setProjection(await api.financeProjection(180))
  }

  if (loading) return <div className="finance-page loading">Loading…</div>

  return (
    <div className="finance-page">
      <BalanceCards accounts={summary.accounts} totalBalance={summary.total_balance} />

      <section>
        <h3>Recurring bills &amp; income</h3>
        <RecurringCharges onChange={refreshProjection} />
      </section>

      <section>
        <h3>Spending by category</h3>
        <SpendingByCategory />
      </section>

      <section>
        <h3>Projected balance (180 days)</h3>
        <ProjectionChart series={projection.series} goals={projection.goals} />
      </section>

      <div className="finance-lower">
        <section>
          <h3>Expected outflows</h3>
          <CalendarGrid />
        </section>

        <section>
          <GoalsList goals={projection.goals} onChange={refreshProjection} />
        </section>
      </div>
    </div>
  )
}
