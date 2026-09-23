// Every section of the app, as a modal over the Command Center.
//
// This used to be a route table feeding a nav sidebar. The app is now one page: the
// board is the app, and a section is something you open on top of it and close again,
// never somewhere you navigate *to* and have to find your way back from. The list is
// still the single registry -- adding a section is still one entry here and nothing
// else -- but `key` replaced `path` and the entry now also says how wide its modal
// wants to be.
//
// `wide` is for sections built around tables and charts (Finance, Media, Email);
// everything else reads fine in the standard column.
import Agenda from './pages/Agenda'
import Agents from './pages/Agents'
import Company from './pages/Company'
import Credit from './pages/Credit'
import Day from './pages/Day'
import DesignLibrary from './pages/DesignLibrary'
import Crypto from './pages/Crypto'
import Email from './pages/Email'
import Finance from './pages/Finance'
import Grocery from './pages/Grocery'
import Kitchen from './pages/Kitchen'
import Media from './pages/Media'
import Needs from './pages/Needs'
import Pipelines from './pages/Pipelines'
import Review from './pages/Review'
import RfAround from './pages/RfAround'
import Schedule from './pages/Schedule'
import Spending from './pages/Spending'
import Tasks from './pages/Tasks'

export const SECTIONS = [
  { key: 'day', label: 'My day', element: Day, wide: true,
    context: 'the Day planner, showing the routine anchors for today, the personal tasks he '
      + 'has chosen for today and the ones he could pick from, what is blocked and on '
      + 'what, and which of his regular habits are slipping' },
  { key: 'tasks', label: 'Tasks', element: Tasks, wide: true,
    context: 'the Tasks section, showing open tasks, their due dates and priorities' },
  { key: 'agenda', label: 'Calendar', element: Agenda, wide: true,
    context: 'the Calendar, one timeline over everything with a date on it: bills and '
      + 'paydays projected forward, tasks due, reminders, credit dispute deadlines and '
      + 'his real calendar, with a running cash position and anything already overdue '
      + 'pinned at the top' },
  { key: 'schedule', label: 'Schedule', element: Schedule, wide: true,
    context: 'the Schedule section, showing upcoming reminders and the weather forecast' },
  { key: 'email', label: 'Email', element: Email, wide: true,
    context: 'the Email section, showing the inbox and flagged threads' },
  { key: 'company', label: 'The Company', element: Company, wide: true,
    context: 'the Company dashboard for the store: how many products a day the team is '
      + 'launching and whether it is paused, what they have listed, what they are planning '
      + 'to make next, what is blocking them, and the written reasoning each agent files '
      + 'in its Obsidian journal explaining how it reached its conclusions' },
  // Labelled for the half he has to act on. It was 'Projects', which is accurate about
  // the second lane and useless for finding the first -- he went looking for the
  // needs-you board and there was no such words anywhere on screen.
  { key: 'needs', label: 'Needs you', element: Needs, wide: true,
    context: 'the Needs You board, showing each business pipeline with two lanes: what the '
      + 'team is blocked on and waiting for him to supply (API keys, accounts, files, '
      + 'purchases, with what each one is blocking and any generation prompts they need '
      + 'him to run), and the business tasks the team is currently working on' },
  { key: 'review', label: 'Review queue', element: Review, wide: true,
    context: 'the Review queue, showing work waiting on your approval' },
  { key: 'kitchen', label: 'Kitchen', element: Kitchen, wide: true,
    context: 'the Kitchen section, showing recipes, pantry inventory and the meal plan' },
  { key: 'grocery', label: 'Grocery', element: Grocery, wide: true,
    context: 'the Grocery section, showing the shopping list and the Kroger cart' },
  { key: 'spending', label: 'Spending', element: Spending, wide: true,
    context: 'the Spending screen, where every item on his checking account is sorted '
      + 'into required or extra so a budget can be built from what he actually spends. '
      + 'It leads with merchants ordered by cost, shows how much is still unsorted, and '
      + 'lets a single transaction override its merchant rule' },
  { key: 'finance', label: 'Finance', element: Finance, wide: true,
    context: 'the Finance section, showing accounts, recurring charges, debts and projections' },
  { key: 'crypto', label: 'Crypto desk', element: Crypto, wide: true,
    context: 'the Crypto desk, showing the paper-trading book, positions and recent trades' },
  { key: 'credit', label: 'Credit', element: Credit, wide: true,
    context: 'the Credit section, showing score history and open disputes' },
  { key: 'pipelines', label: 'Pipelines', element: Pipelines, wide: true,
    context: 'the Pipelines board, showing each product concept from the trend that '
      + 'started it through art, listing and social posts, with the approvals waiting '
      + 'at each stage' },
  { key: 'media', label: 'Media', element: Media, wide: true,
    context: 'the Media section, showing the catalogue by kind and volume' },
  { key: 'library', label: 'Design Library', element: DesignLibrary, wide: true,
    context: 'the Design Library, browsing the print business\'s creative asset shelf by '
      + 'category -- Designs and Local imports hold real data today; Decal icons, Cut '
      + 'files, Footage, Mockups, Backgrounds, Human models, Rooms and STL models are '
      + 'real empty shelves waiting to be filled, not invented content' },
  { key: 'agents', label: 'Office', element: Agents, wide: true,
    context: 'the Office, showing the full staff roster and what each agent is doing' },
  { key: 'rf', label: 'RF / Around the House', element: RfAround, wide: true,
    context: 'the RF / Around the House section, showing the sensor node\'s picture of the '
      + 'street: every transmitter classified (his own sensors, neighbours\' fixtures, '
      + 'passing and recurring vehicles, new/unknown devices), the devices seen in the last '
      + '24 hours with vehicle visit counts and intervals, and the control to flag and name '
      + 'a device as his own' },
]

export const SECTION_BY_KEY = Object.fromEntries(SECTIONS.map((s) => [s.key, s]))
