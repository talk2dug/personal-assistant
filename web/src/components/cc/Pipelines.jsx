import { tintOf } from '../../lib/cc'
import Panel, { PanelEmpty } from './Panel'

/**
 * The two standing pipelines, stage by stage.
 *
 * Every number here is a live row count from the table that actually backs that stage
 * (see _pipelines in routes/command_center.py) — 'Creative 6' means six art briefs are
 * sitting in draft this second, not a figure someone maintains by hand. That is what
 * makes the row worth glancing at: when a stage swells, something upstream of it is
 * producing faster than the next stage is clearing.
 *
 * Stages whose detail exists somewhere open it. Backlog and Building count business_tasks,
 * which for a long time had no screen at all -- the section labelled "Tasks" shows
 * personal_tasks, a different table -- so those two numbers were unopenable and the team's
 * own plan was invisible. They now open the Projects board. A stage with nowhere to go
 * stays inert rather than opening something arbitrary.
 */
const STAGE_SECTION = {
  Backlog: 'needs',
  Building: 'needs',
  'Your call': 'review',
  Listings: 'pipelines',
  Live: 'pipelines',
  Product: 'pipelines',
}

export default function Pipelines({ pipelines, onOpenBusiness, onOpenDev, onOpenSection }) {
  const list = pipelines || []

  return (
    <Panel title="Pipelines" meta="business · development">
      {!pipelines && <PanelEmpty>Loading…</PanelEmpty>}

      {list.map((pipeline, idx) => (
        <div className="cc-pipe" key={pipeline.name}>
          <button
            type="button"
            className="cc-pipe-name"
            onClick={(e) => {
              e.stopPropagation()
              ;(idx === 0 ? onOpenBusiness : onOpenDev)?.()
            }}
          >
            {pipeline.name}
          </button>
          <div className="cc-pipe-stages">
            {pipeline.stages.map((stage) => {
              const target = STAGE_SECTION[stage.label]
              const Tag = target && onOpenSection ? 'button' : 'div'
              return (
                <Tag
                  className={`cc-stage ${target && onOpenSection ? 'is-open' : ''}`}
                  key={stage.label}
                  type={target && onOpenSection ? 'button' : undefined}
                  style={{ borderTopColor: tintOf(stage.k) }}
                  onClick={target && onOpenSection ? (e) => {
                    e.stopPropagation()
                    onOpenSection(target)
                  } : undefined}
                >
                  <span className="cc-stage-label">{stage.label}</span>
                  <span className="cc-stage-count">{stage.count}</span>
                  <span className="cc-stage-note">{stage.note}</span>
                </Tag>
              )
            })}
          </div>
        </div>
      ))}
    </Panel>
  )
}
