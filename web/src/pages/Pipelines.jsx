import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { TINT } from '../lib/cc'
import './pipelines.css'

/**
 * The business team's work, grouped by the product it belongs to.
 *
 * Built because the Review queue could answer "what needs a decision" but never "what is
 * this part of" — twenty-eight pending cards turned out to be nine products at different
 * stages, and nothing said so. The chain was always in the data (art briefs, listings and
 * social posts all carry concept_id); this is the first thing to read it.
 *
 * Layout is the owner's own: the rail on the right lists every product grouped by which
 * track it sells on, and the centre shows one product from the trend that started it
 * through to its social posts, with each stage's pending approvals attached to the thing
 * they are actually about.
 *
 * Artwork is shown, never described. "If they made something I need to be able to see
 * it" — so any review option carrying an image renders the image, via the existing
 * path-checked media endpoint rather than by exposing a server filesystem path.
 */

const STAGE_LABEL = {
  idea: 'The idea', concept: 'Concept', art: 'Art', listing: 'Listing', social: 'Social',
}

const MARKET_LABEL = { local: 'Local market', automated: 'Automated' }

const ORDER = ['idea', 'concept', 'art', 'listing', 'social']

function RailCard({ p, active, onPick }) {
  const reachedIndex = ORDER.indexOf(p.stage)
  return (
    <button type="button" className={`pl-rail-card ${active ? 'is-active' : ''}`} onClick={onPick}>
      <div className="pl-rail-top">
        <span className="pl-rail-name">{p.name}</span>
        {p.pending_reviews > 0 && <span className="pl-wait">{p.pending_reviews}</span>}
      </div>
      <div className="pl-rail-meta">
        <span className="pl-type">{p.product_type || 'unclassified'}</span>
        <span className="pl-dot" />
        <span>{p.counts.art}a · {p.counts.listing}l · {p.counts.social}s</span>
      </div>
      <div className="pl-rail-track">
        {ORDER.map((s, i) => (
          <span key={s} className={`pl-tick ${i <= reachedIndex ? 'is-done' : ''}`} />
        ))}
      </div>
    </button>
  )
}

function ReviewCard({ review }) {
  return (
    <div className="pl-review">
      <div className="pl-review-head">
        <span className="pl-review-flag">Waiting on you</span>
        <span className="pl-review-title">{review.title}</span>
      </div>
      {review.summary && <p className="pl-review-summary">{review.summary}</p>}
      {review.options?.length > 0 && (
        <div className="pl-options">
          {review.options.map((opt) => (
            <figure className="pl-option" key={opt.id}>
              {opt.has_image ? (
                // The server never hands out a filesystem path; this endpoint re-resolves
                // the stored path under generated_media_path before serving a byte.
                <img src={`/api/review/media/${opt.id}`} alt={opt.label || 'option'} />
              ) : (
                <div className="pl-option-noimg">no image</div>
              )}
              <figcaption>
                <span className="pl-option-label">{opt.label}</span>
                {opt.description && <span className="pl-option-desc">{opt.description}</span>}
              </figcaption>
            </figure>
          ))}
        </div>
      )}
      <div className="pl-review-hint">Decide it on the Review page — this is the context it belongs to.</div>
    </div>
  )
}

/** What he picked, kept visible after the card has left the queue. */
function Chosen({ chosen }) {
  if (!chosen) return null
  return (
    <div className={`pl-chosen is-${chosen.decision}`}>
      <span className="pl-chosen-flag">
        {chosen.decision === 'approved' ? 'You picked' : `You ${chosen.decision} this`}
      </span>
      <figure className="pl-option">
        {chosen.has_image
          ? <img src={`/api/review/media/${chosen.id}`} alt={chosen.label || 'chosen'} />
          : <div className="pl-option-noimg">no image</div>}
        <figcaption>
          <span className="pl-option-label">{chosen.label}</span>
          {chosen.description && <span className="pl-option-desc">{chosen.description}</span>}
        </figcaption>
      </figure>
    </div>
  )
}

function ArtItem({ brief }) {
  return (
    <div className="pl-item">
      <div className="pl-item-head">
        <span className="pl-item-title">{brief.title}</span>
        <span className={`pl-status is-${brief.status}`}>{brief.status}</span>
      </div>
      {brief.style_direction && <p className="pl-item-line">{brief.style_direction}</p>}
      <Chosen chosen={brief.chosen} />
      {brief.image_prompt && <p className="pl-prompt">{brief.image_prompt}</p>}
      {brief.reviews?.map((r) => <ReviewCard key={r.id} review={r} />)}
    </div>
  )
}

function ListingItem({ listing }) {
  return (
    <div className="pl-item">
      <div className="pl-item-head">
        <span className="pl-item-title">{listing.title}</span>
        <span className={`pl-status is-${listing.status}`}>{listing.status}</span>
      </div>
      <div className="pl-item-meta">
        {listing.price != null && <span>${Number(listing.price).toFixed(2)}</span>}
        {listing.channel && <span>{listing.channel}</span>}
        {listing.variants?.length > 0 && <span>{listing.variants.length} variants</span>}
      </div>
      {listing.description && <p className="pl-item-line">{listing.description}</p>}
      <Chosen chosen={listing.chosen} />
      {listing.reviews?.map((r) => <ReviewCard key={r.id} review={r} />)}
    </div>
  )
}

function PostItem({ post }) {
  return (
    <div className="pl-item">
      <div className="pl-item-head">
        <span className="pl-item-title">{post.platform}</span>
        <span className={`pl-status is-${post.status}`}>{post.status}</span>
      </div>
      {post.hook && <p className="pl-item-line"><b>{post.hook}</b></p>}
      {post.caption && <p className="pl-item-line">{post.caption}</p>}
      {post.hashtags && <p className="pl-tags">{post.hashtags}</p>}
      <Chosen chosen={post.chosen} />
      {post.reviews?.map((r) => <ReviewCard key={r.id} review={r} />)}
    </div>
  )
}

export default function Pipelines() {
  const [list, setList] = useState(null)
  const [totals, setTotals] = useState({})
  const [filter, setFilter] = useState('all')
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    api.pipelines()
      .then((d) => {
        setList(d.pipelines)
        setTotals(d.totals)
        setSelectedId((cur) => cur ?? (d.pipelines.find((p) => p.pending_reviews)?.id
                                       ?? d.pipelines[0]?.id ?? null))
      })
      .catch((e) => setError(e.message))
  }, [])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    if (selectedId == null) return
    setDetail(null)
    api.pipeline(selectedId).then(setDetail).catch((e) => setError(e.message))
  }, [selectedId])

  async function flipMarket(next) {
    if (!detail) return
    await api.setPipelineMarket(detail.concept.id, next)
    setDetail((d) => ({ ...d, concept: { ...d.concept, market: next } }))
    load()
  }

  const shown = (list || []).filter((p) => filter === 'all' || p.market === filter)
  const grouped = shown.reduce((acc, p) => {
    const key = p.market || 'local'
    ;(acc[key] ||= []).push(p)
    return acc
  }, {})

  return (
    <div className="pl-root">
      <div className="pl-main">
        {error && <p className="cc-panel-err">{error}</p>}
        {!detail && !error && <p className="cc-empty">Select a pipeline.</p>}

        {detail && (
          <>
            <header className="pl-head">
              <div>
                <h2 className="pl-title">{detail.concept.name}</h2>
                <div className="pl-sub">
                  <span className="pl-type">{detail.concept.product_type}</span>
                  <span className="pl-dot" />
                  <span className={`pl-status is-${detail.concept.status}`}>{detail.concept.status}</span>
                  {detail.pending_reviews > 0 && (
                    <>
                      <span className="pl-dot" />
                      <span style={{ color: TINT.warn }}>{detail.pending_reviews} awaiting you</span>
                    </>
                  )}
                </div>
              </div>
              <div className="pl-market-toggle">
                {['local', 'automated'].map((m) => (
                  <button
                    key={m} type="button"
                    className={detail.concept.market === m ? 'is-on' : ''}
                    onClick={() => flipMarket(m)}
                  >
                    {MARKET_LABEL[m]}
                  </button>
                ))}
              </div>
            </header>

            {detail.stages.map((stage) => (
              <section className="pl-stage" key={stage.stage}>
                <div className="pl-stage-head">
                  <span className="pl-stage-name">{STAGE_LABEL[stage.stage]}</span>
                  <span className="cc-rule" />
                  <span className="pl-stage-count">{stage.items.length}</span>
                </div>

                {stage.items.length === 0 && (
                  <p className="pl-empty">Nothing yet at this stage.</p>
                )}

                {stage.stage === 'idea' && stage.items.map((lead, i) => (
                  <div className="pl-item" key={i}>
                    <div className="pl-item-title">{lead.title || lead.topic || 'Trend lead'}</div>
                    {lead.summary && <p className="pl-item-line">{lead.summary}</p>}
                  </div>
                ))}

                {stage.stage === 'concept' && stage.items.map((c) => (
                  <div className="pl-item" key={c.id}>
                    {c.description && <p className="pl-item-line">{c.description}</p>}
                    <div className="pl-item-meta">
                      {c.target_customer && <span>for {c.target_customer}</span>}
                      {c.price_estimate != null && <span>~${Number(c.price_estimate).toFixed(2)}</span>}
                    </div>
                    {c.production_notes && <p className="pl-prompt">{c.production_notes}</p>}
                    <Chosen chosen={c.chosen} />
                  </div>
                ))}

                {stage.stage === 'art' && stage.items.map((b) => <ArtItem key={b.id} brief={b} />)}
                {stage.stage === 'listing' && stage.items.map((l) => <ListingItem key={l.id} listing={l} />)}
                {stage.stage === 'social' && stage.items.map((p) => <PostItem key={p.id} post={p} />)}

                {stage.stage === 'concept' && stage.reviews.map((r) => (
                  <ReviewCard key={r.id} review={r} />
                ))}
              </section>
            ))}
          </>
        )}
      </div>

      <aside className="pl-rail">
        <div className="pl-rail-head">
          <span className="pl-rail-title">Pipelines</span>
          <span className="pl-rail-total">
            {totals.waiting || 0} of {totals.all || 0} waiting
          </span>
        </div>
        <div className="pl-filters">
          {['all', 'local', 'automated'].map((f) => (
            <button key={f} type="button" className={filter === f ? 'is-on' : ''}
                    onClick={() => setFilter(f)}>
              {f === 'all' ? `All ${totals.all || 0}` : `${MARKET_LABEL[f]} ${totals[f] || 0}`}
            </button>
          ))}
        </div>

        <div className="pl-rail-list">
          {list === null && <p className="cc-empty">Loading…</p>}
          {Object.entries(grouped).map(([market, items]) => (
            <div key={market} className="pl-rail-group">
              <div className="pl-rail-group-head">
                {MARKET_LABEL[market]}
                <span className="cc-rule" />
                {items.length}
              </div>
              {items.map((p) => (
                <RailCard key={p.id} p={p} active={p.id === selectedId}
                          onPick={() => setSelectedId(p.id)} />
              ))}
            </div>
          ))}
        </div>
      </aside>
    </div>
  )
}
