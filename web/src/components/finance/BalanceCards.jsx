import CountUp from '../CountUp'

function formatMoney(amount) {
  return (amount ?? 0).toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

const GROUP_LABELS = {
  cash: 'Spendable cash',
  investment: 'Investment / retirement',
  liability: 'Liabilities',
}

function AccountGroup({ groupKey, accounts, total }) {
  if (accounts.length === 0) return null
  return (
    <div className={`balance-group balance-group-${groupKey}`}>
      <div className="balance-group-header">
        <span className="balance-group-label">{GROUP_LABELS[groupKey]}</span>
        <span className="balance-group-total">
          <CountUp value={total ?? 0} format={formatMoney} />
        </span>
      </div>
      <div className="balance-cards">
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
      </div>
    </div>
  )
}

// Groups accounts by the backend's classify_account_type() bucket (see routes/finance.py's
// /summary endpoint, which attaches balance_group per account) instead of blending
// everything into one number — a 401k or a credit card balance both distort "what do I
// actually have to spend" if lumped in with checking/savings.
export default function BalanceCards({ accounts, totalBalance, investmentBalance, liabilityBalance }) {
  const cashAccounts = accounts.filter((a) => a.balance_group === 'cash')
  const investmentAccounts = accounts.filter((a) => a.balance_group === 'investment')
  const liabilityAccounts = accounts.filter((a) => a.balance_group === 'liability')

  return (
    <div className="balance-cards-wrap">
      <AccountGroup groupKey="cash" accounts={cashAccounts} total={totalBalance} />
      <AccountGroup groupKey="investment" accounts={investmentAccounts} total={investmentBalance} />
      <AccountGroup groupKey="liability" accounts={liabilityAccounts} total={liabilityBalance} />

      {accounts.length === 0 && (
        <p className="empty-hint">No account data cached yet — the sync job runs every ~20 min.</p>
      )}
    </div>
  )
}
