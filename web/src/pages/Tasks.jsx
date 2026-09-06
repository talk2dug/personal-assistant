import { useEffect, useState } from 'react'
import { api } from '../api'

function formatDue(iso) {
  if (!iso) return null
  try {
    return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  } catch {
    return iso
  }
}

function TaskRow({ task, onChange }) {
  const done = task.status === 'done'

  async function toggleDone() {
    await api.updatePersonalTask(task.id, { status: done ? 'open' : 'done' })
    onChange()
  }

  async function drop() {
    await api.updatePersonalTask(task.id, { status: 'dropped' })
    onChange()
  }

  return (
    <li className={`task-row priority-${task.priority}${done ? ' is-done' : ''}`}>
      <input type="checkbox" checked={done} onChange={toggleDone} />
      <div className="task-body">
        <div className="task-text">{task.text}</div>
        <div className="task-meta">
          {task.priority !== 'normal' && <span className={`task-priority-tag ${task.priority}`}>{task.priority}</span>}
          {task.due_at && <span>due {formatDue(task.due_at)}</span>}
        </div>
      </div>
      <button className="task-drop" onClick={drop} title="Drop">✕</button>
    </li>
  )
}

function NewTaskForm({ projects, defaultProjectId, onChange }) {
  const [show, setShow] = useState(false)
  const [text, setText] = useState('')
  const [projectId, setProjectId] = useState(defaultProjectId ?? '')
  const [priority, setPriority] = useState('normal')

  async function submit(e) {
    e.preventDefault()
    if (!text.trim()) return
    await api.createPersonalTask({
      text: text.trim(),
      project_id: projectId === '' ? null : Number(projectId),
      priority,
    })
    setText('')
    setPriority('normal')
    setShow(false)
    onChange()
  }

  if (!show) return <button className="task-add-btn" onClick={() => setShow(true)}>+ Add to-do</button>

  return (
    <form className="task-form" onSubmit={submit}>
      <input placeholder="What needs doing?" value={text} onChange={(e) => setText(e.target.value)} autoFocus />
      {projects.length > 0 && (
        <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          <option value="">Unfiled</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      )}
      <select value={priority} onChange={(e) => setPriority(e.target.value)}>
        <option value="low">Low</option>
        <option value="normal">Normal</option>
        <option value="high">High</option>
      </select>
      <div className="task-form-actions">
        <button type="submit">Save</button>
        <button type="button" onClick={() => setShow(false)}>Cancel</button>
      </div>
    </form>
  )
}

function NewProjectForm({ onChange }) {
  const [show, setShow] = useState(false)
  const [name, setName] = useState('')
  const [goal, setGoal] = useState('')

  async function submit(e) {
    e.preventDefault()
    if (!name.trim()) return
    await api.createPersonalProject({ name: name.trim(), goal: goal.trim() || undefined })
    setName('')
    setGoal('')
    setShow(false)
    onChange()
  }

  return (
    <>
      <div className="tasks-header">
        <h3>Projects</h3>
        <button onClick={() => setShow((s) => !s)}>{show ? 'Cancel' : '+ New project'}</button>
      </div>
      {show && (
        <form className="project-form" onSubmit={submit}>
          <input placeholder="Project name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          <input placeholder="What does finishing look like? (optional)" value={goal} onChange={(e) => setGoal(e.target.value)} />
          <button type="submit">Save</button>
        </form>
      )}
    </>
  )
}

function ProjectGroup({ project, tasks, onChange }) {
  const [expanded, setExpanded] = useState(true)
  const openCount = tasks.filter((t) => t.status !== 'done' && t.status !== 'dropped').length

  async function setStatus(status) {
    await api.updatePersonalProject(project.id, { status })
    onChange()
  }

  return (
    <div className={`project-group status-${project.status}`}>
      <div className="project-group-header" onClick={() => setExpanded((e) => !e)}>
        <span className="project-expand">{expanded ? '▾' : '▸'}</span>
        <div className="project-title">
          <span className="project-name">{project.name}</span>
          {project.goal && <span className="project-goal">{project.goal}</span>}
        </div>
        <span className="project-task-count">{openCount} open</span>
        <select
          value={project.status}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="active">active</option>
          <option value="paused">paused</option>
          <option value="done">done</option>
          <option value="dropped">dropped</option>
        </select>
      </div>
      {expanded && (
        <div className="project-group-body">
          {tasks.length === 0 && <p className="empty-hint">Nothing filed here yet.</p>}
          <ul>
            {tasks.map((t) => <TaskRow key={t.id} task={t} onChange={onChange} />)}
          </ul>
          <NewTaskForm projects={[]} defaultProjectId={project.id} onChange={onChange} />
        </div>
      )}
    </div>
  )
}

function ResearchQueue() {
  const [items, setItems] = useState(null)

  async function load() {
    setItems(await api.personalResearch(10))
  }

  useEffect(() => { load() }, [])

  if (items === null) return <p className="empty-hint">Loading…</p>
  if (items.length === 0) return <p className="empty-hint">Nothing delegated yet — ask Jarvis to look something up.</p>

  return (
    <ul className="research-list">
      {items.map((r) => (
        <li key={r.id} className={`research-row status-${r.status}`}>
          <div className="research-row-header">
            <span className="research-topic">{r.topic}</span>
            <span className={`research-status-tag ${r.status}`}>{r.status}</span>
          </div>
          {r.question && <div className="research-question">{r.question}</div>}
          {r.findings && <div className="research-findings">{r.findings}</div>}
        </li>
      ))}
    </ul>
  )
}

export default function Tasks() {
  const [projects, setProjects] = useState(null)
  const [tasks, setTasks] = useState(null)

  async function load() {
    const [p, t] = await Promise.all([api.personalProjects(), api.personalTasks()])
    setProjects(p)
    setTasks(t)
  }

  useEffect(() => { load() }, [])

  if (projects === null || tasks === null) return <div className="tasks-page"><p className="empty-hint">Loading…</p></div>

  const unfiled = tasks.filter((t) => t.project_id == null)

  return (
    <div className="tasks-page">
      <section>
        <NewProjectForm onChange={load} />
        {projects.length === 0 && <p className="empty-hint">No personal projects yet.</p>}
        <div className="project-groups">
          {projects.map((p) => (
            <ProjectGroup
              key={p.id}
              project={p}
              tasks={tasks.filter((t) => t.project_id === p.id)}
              onChange={load}
            />
          ))}
        </div>
      </section>

      <section>
        <h3>Unfiled to-dos</h3>
        {unfiled.length === 0 && <p className="empty-hint">Nothing unfiled.</p>}
        <ul className="task-list">
          {unfiled.map((t) => <TaskRow key={t.id} task={t} onChange={load} />)}
        </ul>
        <NewTaskForm projects={projects} onChange={load} />
      </section>

      <section>
        <h3>Jarvis is working on</h3>
        <ResearchQueue />
      </section>
    </div>
  )
}
