import { useEffect, useState } from 'react'
import { api } from '../api'

function IngredientRow({ ingredient, index, onChange, onRemove }) {
  return (
    <div className="recipe-ingredient-row">
      <input
        placeholder="Ingredient"
        value={ingredient.name}
        onChange={(e) => onChange(index, { ...ingredient, name: e.target.value })}
      />
      <input
        className="recipe-qty"
        placeholder="Qty"
        value={ingredient.quantity || ''}
        onChange={(e) => onChange(index, { ...ingredient, quantity: e.target.value })}
      />
      <input
        className="recipe-unit"
        placeholder="Unit"
        value={ingredient.unit || ''}
        onChange={(e) => onChange(index, { ...ingredient, unit: e.target.value })}
      />
      <button type="button" className="recipe-remove-row" onClick={() => onRemove(index)}>✕</button>
    </div>
  )
}

/** initialDraft, when given (from a photo upload), pre-fills the form and forces it
 *  open -- review-before-save, never auto-saved, since vision extraction from a photo
 *  is genuinely lossy. draft.parsed === false still opens the form (empty, ready for
 *  manual entry) with the model's raw reading shown so nothing is silently discarded. */
function NewRecipeForm({ onChange, initialDraft, onDraftConsumed }) {
  const [show, setShow] = useState(false)
  const [title, setTitle] = useState('')
  const [servings, setServings] = useState('')
  const [ingredients, setIngredients] = useState([{ name: '', quantity: '', unit: '' }])
  const [stepsText, setStepsText] = useState('')
  const [source, setSource] = useState('manual')
  const [photoPath, setPhotoPath] = useState(null)
  const [photoNote, setPhotoNote] = useState(null)

  useEffect(() => {
    if (!initialDraft) return
    setShow(true)
    setSource('photo')
    setPhotoPath(initialDraft.photo_path || null)
    if (initialDraft.parsed) {
      setTitle(initialDraft.title || '')
      setServings(initialDraft.servings ? String(initialDraft.servings) : '')
      setIngredients(
        initialDraft.ingredients?.length ? initialDraft.ingredients : [{ name: '', quantity: '', unit: '' }],
      )
      setStepsText((initialDraft.steps || []).join('\n'))
      setPhotoNote(null)
    } else {
      setPhotoNote(
        `Couldn't fully read that photo (${initialDraft.error}). Here's what it did make out — ` +
        'fill in the rest by hand:\n\n' + (initialDraft.raw_text || '(no text returned)'),
      )
    }
    onDraftConsumed()
  }, [initialDraft])

  function updateIngredient(index, next) {
    setIngredients((prev) => prev.map((ing, i) => (i === index ? next : ing)))
  }
  function removeIngredient(index) {
    setIngredients((prev) => (prev.length > 1 ? prev.filter((_, i) => i !== index) : prev))
  }
  function addIngredientRow() {
    setIngredients((prev) => [...prev, { name: '', quantity: '', unit: '' }])
  }

  function reset() {
    setTitle('')
    setServings('')
    setIngredients([{ name: '', quantity: '', unit: '' }])
    setStepsText('')
    setSource('manual')
    setPhotoPath(null)
    setPhotoNote(null)
    setShow(false)
  }

  async function submit(e) {
    e.preventDefault()
    const cleanIngredients = ingredients.filter((i) => i.name.trim())
    const steps = stepsText.split('\n').map((s) => s.trim()).filter(Boolean)
    if (!title.trim() || cleanIngredients.length === 0 || steps.length === 0) return
    await api.createRecipe({
      title: title.trim(),
      servings: servings ? Number(servings) : undefined,
      ingredients: cleanIngredients,
      steps,
      source,
      photo_path: photoPath || undefined,
    })
    reset()
    onChange()
  }

  if (!show) return <button className="task-add-btn" onClick={() => setShow(true)}>+ Add recipe</button>

  return (
    <form className="task-form recipe-form" onSubmit={submit}>
      {photoNote && <div className="recipe-photo-note">{photoNote}</div>}
      <input placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
      <input
        placeholder="Servings (optional)"
        type="number"
        min="1"
        value={servings}
        onChange={(e) => setServings(e.target.value)}
      />
      <div className="recipe-ingredients-editor">
        <span className="recipe-editor-label">Ingredients</span>
        {ingredients.map((ing, i) => (
          <IngredientRow key={i} ingredient={ing} index={i} onChange={updateIngredient} onRemove={removeIngredient} />
        ))}
        <button type="button" className="recipe-add-row" onClick={addIngredientRow}>+ ingredient</button>
      </div>
      <textarea
        className="recipe-steps-input"
        placeholder="Steps, one per line"
        rows={6}
        value={stepsText}
        onChange={(e) => setStepsText(e.target.value)}
      />
      <div className="task-form-actions">
        <button type="submit">Save</button>
        <button type="button" onClick={reset}>Cancel</button>
      </div>
    </form>
  )
}

function PhotoUploadButton({ onDraft }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function handleFile(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setError('')
    setBusy(true)
    try {
      onDraft(await api.recipeFromPhoto(file))
    } catch (err) {
      setError(err.message || 'Could not read that photo.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <label className={`task-add-btn recipe-photo-btn ${busy ? 'is-busy' : ''}`}>
      {busy ? 'Reading photo…' : '+ Add from photo'}
      <input type="file" accept="image/*" capture="environment" onChange={handleFile} disabled={busy} hidden />
      {error && <span className="recipe-photo-error">{error}</span>}
    </label>
  )
}

function RecipeDetail({ recipe, onClose, onDelete }) {
  return (
    <div className="recipe-detail">
      <div className="recipe-detail-header">
        <h4>{recipe.title}</h4>
        <div className="recipe-detail-actions">
          {recipe.servings ? <span className="recipe-servings">{recipe.servings} servings</span> : null}
          <button className="task-drop" onClick={onClose} title="Close">✕</button>
        </div>
      </div>
      <ul className="recipe-ingredient-list">
        {recipe.ingredients.map((ing, i) => (
          <li key={i}>{[ing.quantity, ing.unit, ing.name].filter(Boolean).join(' ')}</li>
        ))}
      </ul>
      <ol className="recipe-steps-list">
        {recipe.steps.map((step, i) => <li key={i}>{step}</li>)}
      </ol>
      <button className="recipe-delete-btn" onClick={() => onDelete(recipe.id)}>Delete recipe</button>
    </div>
  )
}

export default function Kitchen() {
  const [recipes, setRecipes] = useState(null)
  const [query, setQuery] = useState('')
  const [openId, setOpenId] = useState(null)
  const [photoDraft, setPhotoDraft] = useState(null)

  async function load() {
    setRecipes(await api.kitchenRecipes(query || undefined))
  }

  useEffect(() => { load() }, [query])

  async function deleteRecipe(id) {
    await api.deleteRecipe(id)
    setOpenId(null)
    load()
  }

  if (recipes === null) return <div className="kitchen-page"><p className="empty-hint">Loading…</p></div>

  const openRecipe = recipes.find((r) => r.id === openId)

  return (
    <div className="kitchen-page">
      <section>
        <div className="tasks-header">
          <h3>Recipes</h3>
          <input
            className="recipe-search"
            placeholder="Search titles…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        {recipes.length === 0 && <p className="empty-hint">No recipes saved yet — tell Jarvis one, or add it here.</p>}
        <ul className="task-list">
          {recipes.map((r) => (
            <li key={r.id} className="task-row recipe-row" onClick={() => setOpenId(r.id)}>
              <div className="task-body">
                <div className="task-text">{r.title}</div>
                <div className="task-meta">
                  {r.servings ? <span>{r.servings} servings</span> : null}
                  <span>{r.ingredients.length} ingredients</span>
                </div>
              </div>
            </li>
          ))}
        </ul>
        <NewRecipeForm onChange={load} initialDraft={photoDraft} onDraftConsumed={() => setPhotoDraft(null)} />
        <PhotoUploadButton onDraft={setPhotoDraft} />
      </section>

      {openRecipe && <RecipeDetail recipe={openRecipe} onClose={() => setOpenId(null)} onDelete={deleteRecipe} />}
    </div>
  )
}
