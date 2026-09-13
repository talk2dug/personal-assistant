// Shared department vocabulary and grouping for both agent kinds (built-in AGENT_ROSTER
// and hired staff) -- both already carry a `department` field from the same taxonomy
// (assistant/core/staff.py's CAPABILITY_TIERS), so this is the one place that turns a
// flat agent list into department sections for both the Office page and the Command
// Center's Agents card.
export const DEPARTMENT_LABELS = {
  engineering: 'Engineering',
  design: 'Design',
  creative: 'Creative',
  research: 'Research',
  marketing: 'Marketing',
  commerce: 'Commerce',
  operations: 'Operations',
  general: 'General',
}

export function departmentLabel(department) {
  return DEPARTMENT_LABELS[department] || (department ? department[0].toUpperCase() + department.slice(1) : 'General')
}

/** [[department, [agents...]], ...] sorted by department label, for a section-per-group render. */
export function groupByDepartment(agents) {
  const groups = new Map()
  for (const agent of agents) {
    const dept = agent.department || 'general'
    if (!groups.has(dept)) groups.set(dept, [])
    groups.get(dept).push(agent)
  }
  return [...groups.entries()].sort(([a], [b]) => departmentLabel(a).localeCompare(departmentLabel(b)))
}
