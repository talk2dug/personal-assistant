// The modularity convention for the whole UI: a future agent section is a new page
// component plus one entry here — nothing else needs to change. App.jsx maps this
// list into both the nav sidebar and the router.
import Agents from './pages/Agents'
import Credit from './pages/Credit'
import Crypto from './pages/Crypto'
import Dashboard from './pages/Dashboard'
import Email from './pages/Email'
import Finance from './pages/Finance'
import Media from './pages/Media'
import Review from './pages/Review'
import Grocery from './pages/Grocery'
import Kitchen from './pages/Kitchen'
import Schedule from './pages/Schedule'
import Tasks from './pages/Tasks'

export const sections = [
  // First entry doubles as the default landing route (App.jsx redirects '/' here) --
  // a dashboard is only really a dashboard if it's what you see first.
  { path: '/dashboard', label: 'Command Center', element: Dashboard },
  { path: '/email', label: 'Email', element: Email },
  { path: '/schedule', label: 'Schedule', element: Schedule },
  { path: '/tasks', label: 'Tasks', element: Tasks },
  { path: '/grocery', label: 'Grocery', element: Grocery },
  { path: '/kitchen', label: 'Kitchen', element: Kitchen },
  { path: '/credit', label: 'Credit', element: Credit },
  { path: '/review', label: 'Review', element: Review },
  { path: '/media', label: 'Media', element: Media },
  { path: '/finance', label: 'Finance', element: Finance },
  { path: '/crypto', label: 'Crypto', element: Crypto },
  { path: '/agents', label: 'Office', element: Agents },
]
