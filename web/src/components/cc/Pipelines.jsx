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
 */
export default function Pipelines({ pipelines, onOpenBusiness, onOpenDev }) {
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
            {pipeline.stages.map((stage) => (
              <div
                className="cc-stage"
                key={stage.label}
                style={{ borderTopColor: tintOf(stage.k) }}
              >
                <span className="cc-stage-label">{stage.label}</span>
                <span className="cc-stage-count">{stage.count}</span>
                <span className="cc-stage-note">{stage.note}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </Panel>
  )
}
