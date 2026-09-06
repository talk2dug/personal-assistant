// The modularity convention for the whole UI: a future agent section is a new page
// component plus one entry here â€” nothing else needs to change. App.jsx maps this
// list into both the nav sidebar and the router.
import Agents from './pages/Agents'
import Chat from './pages/Chat'
import Credit from './pages/Credit'
import Crypto from './pages/Crypto'
import Finance from './pages/Finance'
import Media from './pages/Media'
import Review from './pages/Review'
import Grocery from './pages/Grocery'
import Schedule from './pages/Schedule'
import Tasks from './pages/Tasks'

export const sections = [
  { path: '/chat', label: 'Chat', element: Chat },
  { path: '/schedule', label: 'Schedule', element: Schedule },
  { path: '/tasks', label: 'Tasks', element: Tasks },
  { path: '/grocery', label: 'Grocery', element: Grocery },
  { path: '/review', label: 'Review', element: Review },
  { path: '/media', label: 'Media', element: Media },
  { path: '/finance', label: 'Finance', element: Finance },
  { path: '/crypto', label: 'Crypto', element: Crypto },
  { path: '/credit', label: 'Credit', element: Credit },
  { path: '/agents', label: 'Office', element: Agents },
]
