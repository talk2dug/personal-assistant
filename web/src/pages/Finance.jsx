import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import BalanceCards from '../components/finance/BalanceCards'
import ProjectionChart from '../components/finance/ProjectionChart'
import CalendarGrid from '../components/finance/CalendarGrid'
import GoalsList from '../components/finance/GoalsList'
import SpendingByCategory from '../components/finance/SpendingByCategory'
import RecurringCharges from '../components/finance/RecurringCharges'
import SafeToSpendBanner from '../components/finance/SafeToSpendBanner'
import InsightsPanel from '../components/finance/InsightsPanel'
import NetWorthChart from '../components/finance/NetWorthChart'
import DebtsPanel from '../components/finance/DebtsPanel'
import ReconcileQueue from '../components/finance/ReconcileQueue'
import './finance.css'

/**
 * The finance dashboard, rebuilt around what to do rather than what exists.
 *
 * It used to be nine sections stacked vertically — balances, debts, bills, spending,
 * projection, calendar, goals, insights, net worth — every one of them the same size and
 * none of them saying which mattered. That is a reference document, and reading a
 * reference document to find out whether you can afford lunch is exactly the thing an
 * ADHD brain will not do. The data was all correct and almost none of it got looked at.
 *
 * So the top of the page answers three questions in order, and nothing else competes:
 *
 *   How much can I spend?   — one number, the biggest thing on screen
 *   What should I do?       — the planner's single named action, not its whole report
 *   What's blocked on me?   — one card from the reconcile queue, never the whole list
 *
 * Everything that was here before is still here, moved behind tabs. Nothing was removed:
 * it is reference, and reference should be one click away rather than in the way.
 */

const TABS = [
  { key: 'debts', label: 'Debts' },
  { key: 'bills', label: 'Bills & income' },
  { key: 'spending', label: 'Spending' },
  { key: 'future', label: 'Projection & goals' },
  { key: 'accounts', label: 'Accounts' },
]

/** The planner's own headline, lifted server-side so this never parses markdown. */
function DoThisFirst({ planner, onRefresh }) {
  if (!planner?.hired) {
    return (
      <div className="fin-first fin-first-empty">
        <span className="fin-first-tag">No planner hired</span>
        <p>Hire a Financial Planner in the Office and it will tell you what to do first.</p>
      </div>
    )
  }
  if (!planner.one_thing) {
    return (
      <div className="fin-first fin-first-empty">
        <span className="fin-first-tag">Financial Planner</span>
        <p>No report yet — it runs {planner.cadence}. Ask Jarvis to run it now if you
          don't want to wait.</p>
      </div>
    )
  }
  const when = planner.latest?.finished_at?.slice(0, 10)
  return (
    <div className="fin-first">
      <div className="fin-first-head">
        <span className="fin-first-tag">Do this first</span>
        {when && <span className="fin-first-when">Financial Planner · {when}</span>}
      </div>
      <p className="fin-first-body">{planner.one_thing}</p>
      <button type="button" className="fin-btn-quiet" onClick={onRefresh}>Refresh</button>
    </div>
  )
}

export default function Finance() {
  const [summary, setSummary] = useState(null)
  const [projection, setProjection] = useState(null)
  const [planner, setPlanner] = useState(null)
  const [tab, setTab] = useState('debts')
  const [error, setError] = useState(null)

  const loadAll = useCallback(async () => {
    try {
      const [summaryData, projectionData, plannerData] = await Promise.all([
        api.financeSummary(),
        api.financeProjection(180),
        api.financePlanner().catch(() => null),
      ])
      setSummary(summaryData)
      setProjection(projectionData)
      setPlanner(plannerData)
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => { loadAll() }, [loadAll])

  const refreshProjection = useCallback(async () => {
    setProjection(await api.financeProjection(180))
  }, [])

  if (error) return <p className="cc-panel-err">{error}</p>
  if (!summary || !projection) return <div className="finance-page loading">Loading…</div>

  return (
    <div className="finance-page">
      {/* 1. The number. Kept as the existing banner component — it already owns the
          safety-buffer editing, and a second way to set that would be a second answer. */}
      <SafeToSpendBanner data={projection.safe_to_spend} onChange={refreshProjection} />

      {/* 2. The action. */}
      <DoThisFirst planner={planner} onRefresh={loadAll} />

      {/* 3. The question. One card, however many are queued behind it. */}
      <section className="fin-block">
        <h3>Waiting on you</h3>
        <ReconcileQueue onChange={loadAll} />
      </section>

      <nav className="fin-tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t.key} type="button" role="tab" aria-selected={tab === t.key}
                  className={tab === t.key ? 'is-on' : ''} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </nav>

      <div className="fin-tabbody">
        {tab === 'debts' && <DebtsPanel />}

        {tab === 'bills' && <RecurringCharges onChange={refreshProjection} />}

        {tab === 'spending' && (
          <>
            <SpendingByCategory />
            <InsightsPanel />
          </>
        )}

        {tab === 'future' && (
          <>
            <ProjectionChart series={projection.series} goals={projection.goals} />
            <GoalsList goals={projection.goals} onChange={refreshProjection} />
            <CalendarGrid />
          </>
        )}

        {tab === 'accounts' && (
          <>
            <BalanceCards
              accounts={summary.accounts}
              totalBalance={summary.total_balance}
              investmentBalance={summary.investment_balance}
              liabilityBalance={summary.liability_balance}
            />
            <NetWorthChart />
          </>
        )}
      </div>
    </div>
  )
}
