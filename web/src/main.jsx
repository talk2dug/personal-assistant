import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
// Mobile/tablet breakpoints, loaded after index.css on purpose so it can override base
// rules by source order alone (no !important needed except where noted inline). Kept
// as its own file rather than merged into the ~78KB index.css so this responsive pass
// is a clean, reviewable diff on top of styles that already exist and work on desktop.
import './responsive.css'
import App from './App.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
