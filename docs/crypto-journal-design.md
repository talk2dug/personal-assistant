# Design: Persistent Trade/Research Journal for the Crypto Desk

**Project:** Crypto Auto-Trade Pipeline: Freqtrade + CCXT Dry-Run (Phase 1C) - task 16
**Author:** Senior Software Architect
**Date:** 2026-09-12
**Status:** Design for review. No code changes included - this is the spec a developer builds from.
**Storage backend (mandated):** the existing Obsidian vault, via the existing tools only - `write_note`, `read_note`, `list_notes`, `search_notes` - and the existing PARA folder structure (`00-About Me`, `01-Research`, `02-Projects`, `03-Areas`, `04-Journal`, `05-Archive`). No new database, vector store, or bespoke file format.

---

## 0. Problem, restated precisely

Every run of the Crypto Research Analyst and the Crypto Day Trader starts cold:

- The analyst re-derives conclusions it already reached, because nothing carries forward between runs except the live market snapshot.
- The day trader cannot see its own real win rate across its full trade history, and has no memory of *why* past trades won or lost.

Note: `paper_trading.performance()` already computes `win_rate_pct` from the `paper_trades` table today. The gap isn't the arithmetic - it's that this number, and the reasoning behind each trade, never gets fed back into the agent's own prompt on the next run. The fix is memory, not new compute.

---

## 1. Grounding: how this fits what already exists

Read before designing: `obsidian_client.py`, `staff.py` (+ its tests), `paper_trading.py`, `market_data.py`, `web/routes/crypto.py`, `test_engine_obsidian.py`. Two facts drive every decision below:

**(a) These agents run as one-shot completions, not tool loops.** `staff.assign(db, llm, key, standing_assignment)` calls `llm.research(prompt, system_prompt, timeout)` and gets text back. There is no mid-run tool-calling for scheduled staff. Live market data already reaches the prompt this same way: `staff.build_feed_briefing()` pre-fetches from `market_data.py` and splices it into the prompt as text *before* the call. Obsidian's tool-calling wiring (`engine.ObsidianContext`) exists only on the interactive chat path today (per `test_engine_obsidian.py`), not on this scheduled path.

**Implication:** journal retrieval must follow the market-data pattern - the orchestrator pre-fetches from the vault and injects it as a text block in the prompt. We do not need to (and should not) bolt a live tool-calling loop onto unattended timer jobs just for this.

**(b) `write_note` is deliberately append-only.** Per its own docstring: "an existing note is never overwritten... new material lands under a dated subheading." This is correct for a timeline note, but it means a naive "rolling summary" note that agents re-read every run would itself grow forever - the bloat problem just moves one level up the stack.

**Implication:** the rolling summary must be kept small by a small, non-agent-facing housekeeping step, not by asking the agent (or the tool) to overwrite in place. This is not a new store - it operates on the exact same markdown file, through the same filesystem the vault already is. It's the same shape of thing `paper_trading.execute_orders()` already is: the agent never touches the ledger directly, code does.

**(c) The trade ledger (`paper_trades`) and the vault are complementary, not duplicate.** The SQLite ledger is the audit-grade source of truth for P&L arithmetic - exact, fast, queryable, and it must stay that way; nothing here proposes moving numbers into markdown as the system of record. The vault's job is the *narrative* layer: why a trade was taken, what was learned, what the standing thesis is - the part an LLM needs re-injected as language, not the part that needs to balance.

---

## 2. What gets written to the vault after each run

### 2.1 How it gets written (mechanism)

Same pattern as `paper_trading.parse_orders()` / `ORDER_INSTRUCTIONS`: the agent's system prompt asks it to end its prose with a fenced block, e.g.:

```journal
{
  "kind": "analysis" | "trade_outcome" | "lesson",
  "summary": "one-line headline, e.g. 'BTC thesis unchanged: range-bound 74-80k'",
  "direction_change": false,
  "tickers": ["BTC", "ETH"],
  "detail": "full reasoning paragraph(s)",
  "lesson": "optional - what this run confirmed or got wrong about a prior call"
}
```

A new `parse_journal_entry(text)` function (identical shape to `parse_orders`: tolerant of fence labeling, last-block-wins, malformed JSON -> no entry rather than a guessed one) extracts this. The orchestrator - not the LLM - then calls `obsidian.write_note(...)` directly with the parsed, structured content. The agent never calls the Obsidian tool itself during a scheduled run; this matches how it never calls the paper-trading tool itself either. Prose-only output (no block) is valid and simply isn't journaled that run - same philosophy as an empty `orders` list being a legitimate decision.

This also means the journaling requirement is enforced in code, not by hoping the model remembers: if the fenced block is missing on a run where the standing assignment requires one (see 2.3), that's a visible condition the run-loop can log, exactly like a rejected order is logged today.

### 2.2 Where a run's write lands

Each run produces (at most) two writes:

1. **A dated journal entry** in `04-Journal` (always, when the fenced block is present) - the raw, timestamped record.
2. **An update folded into the rolling summary** in `03-Areas` (only when `direction_change` is true, a trade closed, or a `lesson` is present) - the compact, always-current view. Routine "no change" runs write only the dated entry, keeping the summary from growing on every tick.

### 2.3 Schema by content type

**Recommendation / analysis** (`kind: analysis`) - written by the Research Analyst, and by the Day Trader when it explains its market read:
- `summary`, `tickers`, `detail`, `direction_change` (bool - flips true only when this run's call contradicts the last one on file for that ticker), `as_of` (timestamp, filled by orchestrator, not the model - avoids a hallucinated date).

**Trade outcome** (`kind: trade_outcome`) - written by the Day Trader, driven by the orchestrator's own fill/close data (`paper_trading.execute_orders()` result), not by the model's claim about its own trade:
- `code`, `side`, `qty`, `price`, `realized_pnl` (buys carry `realized_pnl: null` - only closes have one), `reason` (the model's stated reason, carried through), `fee`, `quote_age_sec`. All of this already exists in the `paper_trades` row - the journal entry is a narrative wrapper around it, generated by the orchestrator, with an optional one-line `lesson` from the model appended ("stop-losses at -2% would have avoided this").
- On close, the entry links back to the opening entry with a native Obsidian wikilink, e.g. `Closed per [[2026-09-08 -- Crypto Day Trader#14:02 SOL buy]]`, so a round trip is traceable without any new indexing - Obsidian's own backlinks do this for free.

**Lesson learned** (`kind: lesson`) - can be emitted standalone by either agent, not tied to a specific trade ("the 1h-mover feed lags real breakouts by ~10 minutes"). These are exactly what should also land in the rolling summary, since they're precisely the kind of thing that shouldn't need re-deriving every run.

### 2.4 Note titling / append-only handling

`write_note(folder, title, content)` appends under a `## YYYY-MM-DD` heading and only that granularity. To avoid two runs on the same day colliding under one bare date heading, the *content* passed in always leads with the run's own HH:MM timestamp:

```
### 14:02 UTC -- Crypto Day Trader
**BUY SOL** $500 @ $198.40 -- "breakout confirmed on volume"
```

Title convention: one note per agent per calendar day -
`04-Journal/2026-09-12 -- Crypto Research Analyst.md`
`04-Journal/2026-09-12 -- Crypto Day Trader.md`

This keeps file count bounded (2 files/day for the paper desk, regardless of run cadence) while every run within that day is still individually timestamped and append-safe.

---

## 3. Folder / note organization

- **`04-Journal`** - the timeline. One dated note per agent per day, per 2.4. This is the raw material: every run that produced a journal block, in order, forever (or until archived per 3.3). Analogous to `market_history` in `market_data.py` - a full series, not a single "current" value.
- **`03-Areas`** - the running summary. One evergreen note per agent: `03-Areas/Crypto Research Analyst -- Running Summary.md` and `03-Areas/Crypto Day Trader -- Running Summary.md`, plus one umbrella note `03-Areas/Crypto Desk Overview.md` that links to both and to `01-Research`. This is the note re-read into every new run's prompt (see Section 4). It holds: current thesis per major ticker, the last ~10 lessons learned, and machine-computed stats (win rate, trade count, realized P&L) pulled straight from `paper_trading.performance()` at write time - never estimated by the model.
- **`01-Research`** - reserved for durable, deliberately-flagged research ("L2 rotation thesis", "exchange listing patterns"), not routine per-run noise. An agent only writes here when it explicitly says a finding is worth keeping past the daily journal - matches the folder's existing purpose and keeps it from being drowned by routine chatter.
- **`05-Archive`** - where compacted-out history goes (Section 5.3), and, later, where a full quarter of `04-Journal` notes could be swept if the day-trader's daily notes grow long.
- **`00-About Me` / `02-Projects`** - untouched by this design.

---

## 4. Pulling prior entries back into each run's brief

Mirrors exactly how live market data already reaches the prompt (`build_feed_briefing`), so this is one more pre-fetch block, not a new mechanism:

```
STANDING ASSIGNMENT: ...
MARKET DATA: (existing build_feed_briefing output)
COLLEAGUE BRIEFING: (existing build_colleague_briefing output)
PRIOR CONTEXT (new):
  -- read_note(03-Areas, "<Agent> -- Running Summary") in full (bounded by design, see 5.3)
  -- the last N=2 daily notes from 04-Journal for this agent (list_notes + read_note),
     trimmed to their journal-block content, not raw prose
ORDER INSTRUCTIONS: (existing, trader only)
```

`N=2` (today + yesterday) rather than "all history" - the running summary already carries anything older that mattered; recent dated entries add the color of *this week's* runs without re-litigating months of ticks. Both the running-summary read and the recent-entries read are size-capped (e.g. ~2,000 characters combined) at the orchestrator level, the same way market data is capped to top movers rather than all 272 coins.

`search_notes` is used narrowly, not as a general retrieval step: only when the standing assignment or the model's own last output names a specific ticker the desk hasn't looked at in the last two days, to pull any older `01-Research` note on it. This avoids a full-vault linear scan (that's what `search_notes` does today - fine at this volume, worth flagging if the vault grows into the thousands of notes) on every single run.

---

## 5. Keeping the rolling summary from bloating (the append-only problem)

This is the one place the mandate ("existing tools only") and the goal ("don't bloat the prompt over time") pull against each other, so it's worth being explicit:

### 5.1 The tension
`write_note` never overwrites - by design, for safety on proactive capture elsewhere in Jarvis. A summary note fed by `write_note` on every meaningful run would itself become an ever-growing append log, and re-reading "the whole rolling summary note" would eventually be exactly the bloat this design exists to prevent.

### 5.2 Resolution: compaction is a housekeeping job, not an agent action
A small scheduled routine (same category of thing as `market_data.refresh()` - a poller, not a tool the agent calls) runs on a fixed cadence (e.g. weekly, or triggered when the summary note exceeds a size threshold):

1. Read the current `03-Areas` summary note directly off disk (same file, same format - `obsidian_client.py` is explicitly "pure filesystem I/O").
2. Move everything except the most recent compact state into a dated snapshot under `05-Archive` (e.g. `05-Archive/Crypto Day Trader Summary -- 2026-09-12.md`) - so no history is ever deleted, only relocated, preserving Obsidian as the single store.
3. Recompute the compact state (current thesis, last 10 lessons, latest `performance()` stats) and rewrite the summary note's body directly, rather than through the append-only tool call path.
4. This step is ops-level maintenance, exactly like `execute_orders()` already is code the agent never invokes - it doesn't add a new agent-facing tool, and it never touches a note format Obsidian doesn't already understand.

This keeps the promise to the owner intact: still Obsidian, still markdown, still the same folders, still the same four tools from the agent's point of view. The only addition is a small piece of scheduled housekeeping code, not a new store.

### 5.3 If the owner would rather not add even that
The fallback that needs zero new code: treat the rolling summary note as append-only and have the *retrieval* step (4) always read only the text after the **last** `## YYYY-MM-DD` heading, ignoring everything above it. The file still grows on disk, but the prompt injection stays bounded. This is strictly worse for a human skimming the note in Obsidian (older summaries pile up above the fold) and doesn't reclaim the file, so Section 5.2's approach is the recommendation - but it's a legitimate, all-existing-tools fallback if the owner wants literally no new code at all in this area.

---

## 6. Consistency across paper-trading and the future Freqtrade/CCXT dry-run track

Recommendation: **separate identity per execution venue, shared schema, cross-linked summaries.**

- Same journal schema (Section 2.3) and same folder conventions apply to a future "Freqtrade Dry-Run Trader" employee as to today's paper-trading Day Trader. Every entry additionally carries an `venue` field (`paper` | `freqtrade_dryrun` | later `live`) and a matching Obsidian tag, so both `search_notes` and manual browsing can filter by venue.
- Each venue gets its **own** `04-Journal` daily notes and its **own** `03-Areas` running summary (`Crypto Day Trader (Paper) -- Running Summary.md` vs `Crypto Day Trader (Freqtrade Dry-Run) -- Running Summary.md`), each with its own win rate computed from that venue's own fills.
- **Why not one shared summary:** paper fills and dry-run fills will diverge (synthetic fills at cached price vs. Freqtrade's simulated order book / slippage model). The entire point of this project is an honest win rate; merging the two venues' stats into one rolling number would quietly launder a paper-trading result into a claim about the dry-run strategy, or vice versa. Keep them legible separately.
- `03-Areas/Crypto Desk Overview.md` links to both venue summaries plus the analyst's, so the owner still has one place to look. Nothing here is duplicated data - it's just a table of contents note.
- The Research Analyst's output is venue-agnostic (it's market research, not an execution report) and continues to feed both trading employees via the existing colleague-briefing mechanism (`build_colleague_briefing`) - no change needed there.

---

## 7. Summary of what a developer would build (for the next pipeline stage)

1. `parse_journal_entry()` in a new or existing module, mirroring `paper_trading.parse_orders()`.
2. A small addition to the standing-assignment prompt template for the two crypto employees describing the `\`\`\`journal` block format.
3. Orchestrator-side wiring in the crypto run loop: after `llm.research()` returns, parse the block, call `write_note` for the dated entry, and conditionally fold into the running summary per Section 2.2.
4. Orchestrator-side pre-fetch: `read_note` of the running summary + last two daily notes, added to the prompt exactly where `build_feed_briefing`/`build_colleague_briefing` already land.
5. One housekeeping/compaction job per Section 5.2 (or the zero-code fallback in 5.3, owner's call).
6. When Freqtrade/CCXT dry-run is staffed: a second staff record with `venue: freqtrade_dryrun`, its own `04-Journal`/`03-Areas` notes, added to the `Crypto Desk Overview` note.

None of the above touches `paper_trading.py`'s ledger arithmetic, the market data poller, or the CCXT credential boundary - it is purely an additive narrative layer on top of what already exists.

---

## 8. Open questions for the owner / next stage

- Should the Day Trader journal *every* run, or only runs with a trade, a direction change, or an explicit lesson (recommended, to match its frequent cadence)? The Analyst, running less often, can reasonably journal every run.
- Weekly compaction cadence (Section 5.2) - is weekly right, or should it trigger on a note-size threshold instead?
- Naming convention for the future Freqtrade employee's `staff_key` / title, for continuity with `Crypto Desk Overview.md`.
