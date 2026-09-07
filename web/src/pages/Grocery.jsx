import { useEffect, useState } from 'react'
import { api } from '../api'

function formatWhen(iso) {
  if (!iso) return null
  try {
    return new Date(iso).toLocaleString('en-US', {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function CartPanel() {
  const [cart, setCart] = useState(null)
  const [error, setError] = useState(null)

  async function load() {
    try {
      setCart(await api.groceryCart())
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => { load() }, [])

  async function handleClear() {
    await api.clearGroceryCart()
    load()
  }

  if (error) return <p className="empty-hint">Cart unavailable — {error}</p>
  if (cart === null) return <p className="empty-hint">Loading…</p>

  const items = cart.current_cart || []

  return (
    <div className="cart-panel">
      <div className="cart-note">
        Jarvis can only see what it has added here — not your real Kroger cart, which
        only Kroger's own app can show. Clear this once you've checked out for real.
      </div>
      {items.length === 0 && <p className="empty-hint">Nothing staged.</p>}
      {items.length > 0 && (
        <>
          <ul className="cart-items">
            {items.map((item, i) => (
              <li key={i} className="cart-item-row">
                <span>{item.product_id}</span>
                <span className="cart-item-qty">×{item.quantity}</span>
                <span className="cart-item-modality">{item.modality}</span>
              </li>
            ))}
          </ul>
          <button className="cart-clear-btn" onClick={handleClear}>I checked out — clear this</button>
        </>
      )}
    </div>
  )
}

function StorePicker() {
  const [preferred, setPreferred] = useState(null)
  const [zip, setZip] = useState('')
  const [results, setResults] = useState(null)
  const [busy, setBusy] = useState(false)

  async function loadPreferred() {
    try {
      setPreferred(await api.preferredGroceryStore())
    } catch {
      setPreferred({ success: false })
    }
  }

  useEffect(() => { loadPreferred() }, [])

  async function search(e) {
    e.preventDefault()
    setBusy(true)
    try {
      const res = await api.searchGroceryStores(zip.trim() || undefined)
      setResults(res.data || [])
    } finally {
      setBusy(false)
    }
  }

  async function choose(locationId) {
    await api.setPreferredStore(locationId)
    setResults(null)
    loadPreferred()
  }

  return (
    <div className="store-picker">
      {preferred?.success ? (
        <div className="store-current">
          Your store: <strong>{preferred.location_details?.name}</strong> — {preferred.location_details?.address?.addressLine1}
        </div>
      ) : (
        <p className="empty-hint">No store set yet — search below and pick one before searching products or building a cart.</p>
      )}
      <form className="store-search-form" onSubmit={search}>
        <input placeholder="Zip code" value={zip} onChange={(e) => setZip(e.target.value)} />
        <button type="submit" disabled={busy}>{busy ? 'Searching…' : 'Find stores'}</button>
      </form>
      {results && (
        <ul className="store-results">
          {results.length === 0 && <li className="empty-hint">No stores found.</li>}
          {results.map((s) => (
            <li key={s.location_id} className="store-result-row">
              <div>
                <div className="store-name">{s.name}</div>
                <div className="store-address">{s.full_address}</div>
              </div>
              <button onClick={() => choose(s.location_id)}>Use this store</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function RecipeBuilder({ onCartChanged }) {
  const [dish, setDish] = useState('')
  const [servings, setServings] = useState('')
  const [ingredientsText, setIngredientsText] = useState('')
  const [matches, setMatches] = useState(null)
  const [checked, setChecked] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function propose(e) {
    e.preventDefault()
    const ingredients = ingredientsText.split('\n').map((s) => s.trim()).filter(Boolean)
    if (!dish.trim() || ingredients.length === 0) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.proposeRecipe({
        dish: dish.trim(), servings: servings ? Number(servings) : undefined, ingredients,
      })
      setMatches(result.matches || [])
      setChecked(Object.fromEntries((result.matches || []).map((m, i) => [i, m.matched])))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function confirm() {
    const items = (matches || [])
      .map((m, i) => ({ m, i }))
      .filter(({ m, i }) => m.matched && checked[i])
      .map(({ m }) => ({ product_id: m.product_id, quantity: 1, modality: 'PICKUP' }))
    if (items.length === 0) return
    setBusy(true)
    try {
      await api.confirmRecipe(items)
      setMatches(null)
      setDish('')
      setIngredientsText('')
      onCartChanged()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="recipe-builder">
      {matches === null ? (
        <form className="recipe-form" onSubmit={propose}>
          <input placeholder="What do you want to make? (e.g. chili)" value={dish} onChange={(e) => setDish(e.target.value)} />
          <input placeholder="Servings (optional)" type="number" value={servings} onChange={(e) => setServings(e.target.value)} />
          <textarea
            placeholder={'One ingredient per line, e.g.\nground beef\nkidney beans\nchili powder'}
            value={ingredientsText}
            onChange={(e) => setIngredientsText(e.target.value)}
            rows={5}
          />
          <button type="submit" disabled={busy}>{busy ? 'Searching…' : 'Find ingredients'}</button>
        </form>
      ) : (
        <div className="recipe-review">
          <p className="empty-hint">Review the matches below — nothing has been added to the cart yet.</p>
          <ul className="recipe-matches">
            {matches.map((m, i) => (
              <li key={i} className={`recipe-match-row${m.matched ? '' : ' no-match'}`}>
                {m.matched ? (
                  <>
                    <input type="checkbox" checked={!!checked[i]} onChange={(e) => setChecked({ ...checked, [i]: e.target.checked })} />
                    <div className="recipe-match-body">
                      <div className="recipe-match-ingredient">{m.ingredient}</div>
                      <div className="recipe-match-product">{m.description} {m.size ? `(${m.size})` : ''} {m.price || ''}</div>
                    </div>
                  </>
                ) : (
                  <div className="recipe-match-body">
                    <div className="recipe-match-ingredient">{m.ingredient}</div>
                    <div className="recipe-match-unmatched">no good match found</div>
                  </div>
                )}
              </li>
            ))}
          </ul>
          <div className="recipe-review-actions">
            <button onClick={confirm} disabled={busy}>{busy ? 'Adding…' : 'Add checked items to cart'}</button>
            <button className="recipe-cancel" onClick={() => setMatches(null)}>Cancel</button>
          </div>
        </div>
      )}
      {error && <p className="empty-hint">{error}</p>}
    </div>
  )
}

export default function Grocery() {
  const [cartKey, setCartKey] = useState(0)

  return (
    <div className="grocery-page">
      <section>
        <h3>Store</h3>
        <StorePicker />
      </section>
      <section>
        <h3>Make Something</h3>
        <RecipeBuilder onCartChanged={() => setCartKey((k) => k + 1)} />
      </section>
      <section>
        <h3>Cart</h3>
        <CartPanel key={cartKey} />
      </section>
    </div>
  )
}
