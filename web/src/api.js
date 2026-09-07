// Thin fetch wrapper for the FastAPI backend. Session auth is a cookie, so every
// call needs credentials: 'include' (Vite's dev proxy keeps it same-origin locally;
// in production this is served from the same origin as the API, so it's moot there
// but harmless to always include).

class ApiError extends Error {
  constructor(status, body) {
    super(body?.detail || `Request failed (${status})`)
    this.status = status
    this.body = body
  }
}

async function request(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'include',
    headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
    ...options,
  })
  if (!res.ok) {
    let body = null
    try {
      body = await res.json()
    } catch {
      // no JSON body
    }
    throw new ApiError(res.status, body)
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  login: (name, password) =>
    request('/api/login', { method: 'POST', body: JSON.stringify({ name, password }) }),
  logout: () => request('/api/logout', { method: 'POST' }),
  me: () => request('/api/me'),

  chatHistory: () => request('/api/chat/history'),
  sendMessage: (text, image) =>
    request('/api/chat/message', { method: 'POST', body: JSON.stringify(image ? { text, image } : { text }) }),
  // FormData, not JSON — must NOT go through request()'s helper, which force-sets
  // Content-Type: application/json; the browser needs to set the multipart boundary itself.
  transcribe: async (audioBlob) => {
    const form = new FormData()
    form.append('audio', audioBlob, 'recording.webm')
    const res = await fetch('/api/chat/transcribe', { method: 'POST', credentials: 'include', body: form })
    if (!res.ok) {
      const body = await res.json().catch(() => null)
      throw new ApiError(res.status, body)
    }
    return res.json()
  },

  agentStatus: () => request('/api/agents/status'),

  cryptoDashboard: () => request('/api/crypto/dashboard'),

  scheduleReminders: (scope) => request(`/api/schedule/reminders${scope ? `?scope=${scope}` : ''}`),
  createReminder: (reminder) =>
    request('/api/schedule/reminders', { method: 'POST', body: JSON.stringify(reminder) }),
  deleteReminder: (id) => request(`/api/schedule/reminders/${id}`, { method: 'DELETE' }),
  schedulePlaces: () => request('/api/schedule/places'),
  createPlace: (place) => request('/api/schedule/places', { method: 'POST', body: JSON.stringify(place) }),
  deletePlace: (id) => request(`/api/schedule/places/${id}`, { method: 'DELETE' }),

  weatherNow: (forecastType = 'daily') => request(`/api/weather?forecast_type=${forecastType}`),

  personalProjects: (status) => request(`/api/personal/projects${status ? `?status=${status}` : ''}`),
  createPersonalProject: (project) =>
    request('/api/personal/projects', { method: 'POST', body: JSON.stringify(project) }),
  updatePersonalProject: (id, patch) =>
    request(`/api/personal/projects/${id}`, { method: 'PUT', body: JSON.stringify(patch) }),
  personalTasks: ({ status, projectId } = {}) => {
    const params = new URLSearchParams()
    if (status) params.set('status', status)
    if (projectId != null) params.set('project_id', projectId)
    const qs = params.toString()
    return request(`/api/personal/tasks${qs ? `?${qs}` : ''}`)
  },
  createPersonalTask: (task) =>
    request('/api/personal/tasks', { method: 'POST', body: JSON.stringify(task) }),
  updatePersonalTask: (id, patch) =>
    request(`/api/personal/tasks/${id}`, { method: 'PUT', body: JSON.stringify(patch) }),
  // status is optional -- the dashboard's Active Work panel asks for 'requested' only
  // (still in flight); Tasks.jsx asks for everything so it can show recent findings too.
  personalResearch: (limit = 10, status) =>
    request(`/api/personal/research?limit=${limit}${status ? `&status=${status}` : ''}`),
  requestPersonalResearch: (research) =>
    request('/api/personal/research', { method: 'POST', body: JSON.stringify(research) }),

  groceryCart: () => request('/api/grocery/cart'),
  clearGroceryCart: () => request('/api/grocery/cart/clear', { method: 'POST' }),
  groceryPantry: (status) => request(`/api/grocery/pantry${status ? `?status=${status}` : ''}`),
  upsertPantryItem: (item) => request('/api/grocery/pantry', { method: 'POST', body: JSON.stringify(item) }),
  deletePantryItem: (id) => request(`/api/grocery/pantry/${id}`, { method: 'DELETE' }),
  searchGroceryStores: (zipCode) =>
    request(`/api/grocery/stores${zipCode ? `?zip_code=${zipCode}` : ''}`),
  preferredGroceryStore: () => request('/api/grocery/stores/preferred'),
  setPreferredStore: (locationId) =>
    request('/api/grocery/stores/preferred', { method: 'POST', body: JSON.stringify({ location_id: locationId }) }),
  proposeRecipe: (recipe) =>
    request('/api/grocery/recipe/propose', { method: 'POST', body: JSON.stringify(recipe) }),
  confirmRecipe: (items) =>
    request('/api/grocery/recipe/confirm', { method: 'POST', body: JSON.stringify({ items }) }),

  kitchenRecipes: (query) => request(`/api/kitchen/recipes${query ? `?query=${encodeURIComponent(query)}` : ''}`),
  createRecipe: (recipe) => request('/api/kitchen/recipes', { method: 'POST', body: JSON.stringify(recipe) }),
  getRecipe: (id) => request(`/api/kitchen/recipes/${id}`),
  updateRecipe: (id, patch) => request(`/api/kitchen/recipes/${id}`, { method: 'PUT', body: JSON.stringify(patch) }),
  deleteRecipe: (id) => request(`/api/kitchen/recipes/${id}`, { method: 'DELETE' }),
  // FormData, not JSON — same reasoning as transcribe above.
  recipeFromPhoto: async (file) => {
    const form = new FormData()
    form.append('photo', file, file.name)
    const res = await fetch('/api/kitchen/recipes/from-photo', { method: 'POST', credentials: 'include', body: form })
    if (!res.ok) {
      const body = await res.json().catch(() => null)
      throw new ApiError(res.status, body)
    }
    return res.json()
  },

  reviewItems: (status = 'pending') => request(`/api/review/items?status=${status}`),
  decideReview: (id, decision) =>
    request(`/api/review/items/${id}/decide`, { method: 'POST', body: JSON.stringify(decision) }),

  mediaSummary: () => request('/api/media/summary'),
  mediaHosts: () => request('/api/media/hosts'),
  mediaFolders: ({ minFiles = 1, kind = '', decision = '', limit = 500, offset = 0 } = {}) =>
    request(`/api/media/folders?min_files=${minFiles}&kind=${kind}&decision=${decision}` +
            `&limit=${limit}&offset=${offset}`),
  decideMediaFolder: (id, decision, notes = '') =>
    request(`/api/media/folders/${id}/decide`, {
      method: 'POST', body: JSON.stringify({ decision, notes }),
    }),
  decideMediaFoldersBulk: (ids, decision) =>
    request('/api/media/folders/decide-bulk', {
      method: 'POST', body: JSON.stringify({ ids, decision }),
    }),
  mediaCollection: () => request('/api/media/collection'),
  mediaArchives: (unreadable = false) =>
    request(`/api/media/archives?unreadable=${unreadable}`),
  mediaDuplicates: () => request('/api/media/duplicates'),

  financeSummary: () => request('/api/finance/summary'),
  financeProjection: (horizonDays = 180) =>
    request(`/api/finance/projection?horizon_days=${horizonDays}`),
  financeCalendar: (start, end) =>
    request(`/api/finance/calendar?start=${start}&end=${end}`),
  listGoals: () => request('/api/finance/goals'),
  createGoal: (goal) =>
    request('/api/finance/goals', { method: 'POST', body: JSON.stringify(goal) }),
  updateGoal: (id, patch) =>
    request(`/api/finance/goals/${id}`, { method: 'PUT', body: JSON.stringify(patch) }),
  deleteGoal: (id) => request(`/api/finance/goals/${id}`, { method: 'DELETE' }),

  financeSpending: (period = 'this_month') => request(`/api/finance/spending?period=${period}`),
  listBudgets: () => request('/api/finance/budgets'),
  createBudget: (budget) =>
    request('/api/finance/budgets', { method: 'POST', body: JSON.stringify(budget) }),
  deleteBudget: (id) => request(`/api/finance/budgets/${id}`, { method: 'DELETE' }),

  listRecurring: () => request('/api/finance/recurring'),
  createManualRecurring: (charge) =>
    request('/api/finance/recurring/manual', { method: 'POST', body: JSON.stringify(charge) }),
  deleteManualRecurring: (id) => request(`/api/finance/recurring/manual/${id}`, { method: 'DELETE' }),
  setEraRecurringExcluded: (chargeKey, excluded) =>
    request(`/api/finance/recurring/era/${encodeURIComponent(chargeKey)}`, {
      method: 'PUT',
      body: JSON.stringify({ excluded }),
    }),
}

export { ApiError }
