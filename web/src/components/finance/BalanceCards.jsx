import CountUp from '../CountUp'

function formatMoney(amount) {
  return (amount ?? 0).toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

export default function BalanceCards({ accounts, totalBalance }) {
  return (
    <div className="balance-cards">
      <div className="card total-card">
        <div className="card-label">Total balance</div>
        <div className="card-amount">
          <CountUp value={totalBalance ?? 0} format={formatMoney} />
        </div>
      </div>
      {accounts.map((a) => (
        <div className="card" key={a.account_key}>
          <div className="card-label">
            {a.name} <span className="account-type">{a.account_type}</span>
          </div>
          <div className="card-amount">
            <CountUp value={a.balance ?? 0} format={formatMoney} />
          </div>
        </div>
      ))}
      {accounts.length === 0 && (
        <p className="empty-hint">No account data cached yet — the sync job runs every ~20 min.</p>
      )}
    </div>
  )
}
