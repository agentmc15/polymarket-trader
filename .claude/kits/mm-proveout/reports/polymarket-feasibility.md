# Polymarket feasibility — can forward collection ever reach power?

**This is a feasibility study, not a strategy test. No P&L is modeled and no profit is estimated
anywhere below.** The question answered is narrower and comes first: if the kit spends weeks
collecting Polymarket order-book snapshots, can that collection structurally reach the same kind
of statistical power Kalshi's 1-minute holdout reached (1,477 trading markets across 1,057 events,
measured 2026-09-07 to 2026-09-08 — `reports/kalshi-holdout.md`, T18c)? **At `min_spread=0.25`
(the current `MarketMaker` default, itself under review), the answer measured today is
effectively NEVER within any timeframe this kit could fund: the fastest defensible scenario is
~4 years, built on an optimistic assumption this snapshot cannot verify.** At `min_spread=0.10`
(the collection floor, `settings.book_collection_min_spread`) the fastest defensible scenario is
~2.4 years — faster, but still an order of magnitude past a multi-week or multi-month collection
budget. Both numbers are reported side by side; this report does not recommend a threshold.

**Why market count alone was never going to answer this.** The Phase 1 review that rejected an
earlier Kalshi verdict did so because two disjoint-by-market-id samples turned out to share 3,183
events — event identity, not market identity, is the unit of statistical independence in this kit's
own accepted method (GUARDRAILS.md §2.4). Measured live today, Polymarket's quotable universe at
`min_spread=0.25, min_volume=100` is 75 markets spanning only **32 distinct events** (largest event
17.3% of the set) — an order of magnitude smaller, on BOTH axes, than Kalshi's 643-quotable /
1,057-event holdout. And the single biggest structural fact this report surfaces — the close-time
distribution of today's quotable cohort — shows that market count would not even start growing
soon: roughly 90% of today's quotable markets share essentially one close date, **2026-12-31**, over
100 days out, with a hard gap and no markets closing in between.

Every figure below carries its own measurement timestamp. Nothing here is a constant of the venue;
it is one day's live reading, taken 2026-09-12, of a market whose composition visibly moved between
two snapshots 37 minutes apart.

---

## 0. Scope, method, and what this report will not do

- **Method**: every count below comes from the repo's own `app.venues.types.quotable_spread()`,
  `app.venues.types.venue_volume()`, and `app.services.data_collector.select_quotable_markets()` —
  never reimplemented. `select_quotable_markets` reads `settings.book_collection_min_spread` /
  `settings.book_collection_min_volume` at call time; the grid in §1 was produced by setting those
  two values for each cell and calling the real function, then restoring the originals — the
  partition logic itself was never touched or copied.
- **Network**: read-only public GETs only — `MarketDataAdapter.list_markets()` (Gamma `/markets`,
  `/events`) and `MarketDataAdapter.get_book()` (CLOB `/book`), via
  `app.api.deps.get_market_data_adapters()`, the same read-only-adapter seam
  `app.scripts.probe_quotable` uses. No order was placed, modified, or cancelled.
  `TRADING_MODE`/`LIVE_TRADING_CONFIRMATION` were never touched, and `.env` was never opened
  directly by any script in this task.
- **What this report does NOT do**: no P&L, no ROC, no per-fill edge, no profit estimate, no
  threshold recommendation. Where §1 shows `min_spread=0.10` produces a much bigger quotable
  universe than `min_spread=0.25`, that is reported as a fact for the user to weigh against
  `min_spread=0.25`'s own (separately measured, Kalshi-only) adverse-selection rationale — this
  report does not choose between them.
- **Structural vs. weather** — flagged inline throughout, and summarized in §6.

---

## 1. Quotable universe grid

**Measured 2026-09-12T15:55:42Z–15:55:51Z UTC** (one `list_markets(status="open")` walk, ~10s).
`listed_open_markets=1,919`, `two_sided=1,645` (Gamma's own listing walk is capped at ~2,100 raw
items — confirmed live the same day: `GET /markets?offset=2090` returns HTTP 422 `"offset too
large, use /markets/keyset for deeper pagination"`, a venue-side ceiling `app/venues/polymarket/
adapter.py`'s `_MAX_GAMMA_PAGES` already documents. This is a structural cap on the walk, not
necessarily on the open-market count: the 2026-09-07 measurement in `NOTES.md` (T7) found
1,918 listed 5 days earlier — near-identical — so the open-market snapshot does not look like it
is being meaningfully truncated by the ceiling in practice, but the walk cannot prove a negative
here.)

| spread ≥ | volume ≥ | quotable | selected (`top_n=500`) | n_events | largest event share |
|---|---|---|---|---|---|
| 0.02 | 0 | 564 | **500 (capped)** | 217 | 4.6% |
| 0.02 | 100 | 449 | 449 | 180 | 4.5% |
| 0.02 | 1,000 | 342 | 342 | 149 | 5.3% |
| 0.05 | 0 | 263 | 263 | 97 | 9.9% |
| 0.05 | 100 | 208 | 208 | 80 | 8.7% |
| 0.05 | 1,000 | 149 | 149 | 65 | 8.1% |
| 0.10 | 0 | 170 | 170 | 67 | 15.3% |
| **0.10** | **100** | **130** | **130** | **53** | **12.3%** |
| 0.10 | 1,000 | 88 | 88 | 42 | 9.1% |
| 0.15 | 0 | 141 | 141 | 61 | 17.7% |
| 0.15 | 100 | 106 | 106 | 47 | 14.2% |
| 0.15 | 1,000 | 71 | 71 | 37 | 9.9% |
| 0.25 | 0 | 104 | 104 | 43 | 19.2% |
| **0.25** | **100** | **75** | **75** | **32** | **17.3%** |
| 0.25 | 1,000 | 48 | 48 | 24 | 12.5% |

Bolded rows are the two headline configurations this report compares throughout: the current
production collection floor (`book_collection_min_spread=0.10`, `book_collection_min_volume=100`)
and `MarketMaker`'s current, under-review `min_spread=0.25` at the same volume floor. Only the
loosest cell (`spread≥0.02, volume≥0`) exceeds `book_collection_top_n=500`; every other cell,
including both headline rows, is entirely below the cap — the cap never binds at production
settings today.

**Kalshi, for scale (measured 2026-09-07, `NOTES.md` T7 — a different day, shown for context, not
as a live comparison)**: 96,072 listed → 60,332 two-sided → 643 quotable → 500 selected (`top_n`
DOES bind on Kalshi). Polymarket's entire quotable universe at the loosest grid cell (564) is
smaller than Kalshi's `quotable` count (643) alone, and at the two headline cells (75, 130) is
roughly an order of magnitude smaller.

---

## 2. Event concentration — the number that actually decides this

Same snapshot as §1; `n_events`/`largest event share` columns above are the full answer, repeated
here with the comparison that matters:

| | quotable markets | distinct events | largest event share | markets per event |
|---|---|---|---|---|
| Polymarket, `min_spread=0.25, vol≥100` | 75 | **32** | 17.3% | 2.34 |
| Polymarket, `min_spread=0.10, vol≥100` | 130 | **53** | 12.3% | 2.45 |
| Kalshi 1-min holdout candidate (`min_spread=0.25`), measured 2026-09-08 | 1,477 trading | **1,057** | not reported at this grain | 1.40 |

Two things follow directly, and neither is a matter of opinion:

1. **Polymarket's event count is 20–33× smaller than the Kalshi holdout that produced a real
   interval**, at either threshold. Even if market count reached 1,000 someday, 30–50 events is
   the plausible ceiling this snapshot suggests, not 1,000 — nowhere close to Kalshi's 1,057.
2. **Polymarket's markets are, if anything, MORE concentrated per event than Kalshi's** (2.3–2.5
   markets/event vs. Kalshi's 1.40) — consistent with Polymarket's habit of bundling many
   candidates into one multi-outcome event (an election, an award), which is exactly the structure
   GUARDRAILS.md's clustering rule (§2.4) exists to correct for. A naive per-market interval on
   Polymarket would overstate its effective sample size by roughly the same 2.3–2.5× Kalshi's own
   review just demonstrated matters.

`≥30 distinct events` (this kit's own floor) is already cleared in a single snapshot at both
headline thresholds (32 and 53). **Event count is not the binding constraint here — market count
is**, and §5 is about why market count grows far slower than a first glance at §1 suggests.

---

## 3. Fee and rebate reality

Sampled from the same 2026-09-12T15:55Z snapshot, over the raw `feeSchedule` object on the listing
payload (never `market.fee`, the already-parsed/fallback-capable value, for this table — the raw
object is what "published" means) and cross-checked against the parsed `FeeSchedule.source`.

| | `min_spread=0.10, vol≥100` (n=130) | `min_spread=0.25, vol≥100` (n=75) |
|---|---|---|
| carries a raw `feeSchedule` object | 128 (98.5%) | 73 (97.3%) |
| `feesEnabled: false` (no fee, no rebate — an explicit answer) | 2 (1.5%) | 2 (2.7%) |
| `rate` distribution | 0.04: 52 (40.0%) · 0.07: 49 (37.7%) · 0.03: 14 (10.8%) · 0.05: 13 (10.0%) | 0.04: 31 (41.3%) · 0.07: 26 (34.7%) · 0.03: 8 (10.7%) · 0.05: 8 (10.7%) |
| `takerOnly` | `true` on all 128 with a schedule | `true` on all 73 with a schedule |
| `rebateRate` distribution | 0.25: 79 (60.8%) · 0.20: 49 (37.7%) · (0 via `feesEnabled=false`): 2 (1.5%) | 0.25: 47 (62.7%) · 0.20: 26 (34.7%) · (0): 2 (2.7%) |
| parsed `FeeSchedule.source` | `venue_schedule`: 130/130 | `venue_schedule`: 75/75 |

**Every market this collection would actually quote at either threshold parses through
`_published_fee_schedule` to `source="venue_schedule"` — none fall back to the hand-maintained
category table.** And a nonzero maker rebate (20% or 25% of the taker fee) is published on 96–99%
of them, split roughly 60/40 between the two rates; the remaining 1.5–2.7% explicitly disable fees
entirely (0 fee, 0 rebate), never silently default to one.

**What this does and does not establish.** This is a public GET against the listing payload — it
proves the rebate rate is *published* by the venue, on the specific markets this collection would
select, today. It proves nothing about whether a rebate is *paid*: Polymarket settles rebates
separately (daily, in pUSD, per `app/venues/types.py`'s `FeeSchedule.maker_rebate_rate` docstring),
under terms the venue can change, and no public GET can observe a settlement. That question needs a
live fill and a later balance check — out of scope here, and out of scope for any report this kit
produces without one.

---

## 4. Book dwell time

**Measured 2026-09-12T16:00:43Z–16:15:44Z UTC** — 16 polls at 60-second spacing (15 minutes wall
clock), against the top 30 markets by volume from the `min_spread=0.10, vol≥100` quotable set
(the broadest of the two headline sets). 480 `get_book` reads, **0 errors**. `OrderBook.ts` is
Polymarket's own book-last-moved timestamp (`PolymarketAdapter._parse_book`), not poll time, so a
repeated `ts` across consecutive polls means the book genuinely did not move and collection at that
cadence would write no new row for that beat.

| | value |
|---|---|
| 60-second interval change rate (450 intervals) | 219 changed → **48.7%** |
| 180-second window change rate (150 windows, i.e. the production collection beat) | 102 changed → **68.0%** |
| markets with zero book movement over the full 15 minutes | 3 / 30 (10%) |
| markets that changed on every single 60s poll | 10 / 30 (33%) |

Per-market `change_rate_180s` spans the full range, from 0.0 (three markets, volumes $18.8K–$276K,
never moved) to 1.0 (ten markets, changed every window). There is no clean volume threshold visible
in this sample — one of the three static markets (`vol=$275,654`) is the third-highest-volume
candidate in the set, so high listed volume alone did not guarantee book movement in this 15-minute
window.

**What this settles and what it does not.** At the current 180-second collection beat, roughly
two-thirds of polls capture a genuinely new book — the beat is not badly mismatched to how fast
Polymarket books move, and a shorter beat would mostly re-observe the same 32% of unchanged books
rather than capture much new information. But dwell time answers a *different* question from §5's
headline: it says how much depth history accrues **per market, per day**, once a market is being
tracked — it says nothing about how many *calendar days* pass before a tracked market closes and
resolves into a "trading" market. That is entirely a function of §1/§2's population and the
close-time structure in §5, not of poll cadence.

---

## 5. Time-to-power: the headline

### 5.1 The structural fact that sets the pace: close-time clustering

**Measured 2026-09-12T16:32:24Z UTC** (a fresh `list_markets(status="open")` walk, ~37 minutes
after §1's snapshot — the count moved slightly in that gap: 75→76 quotable at `min_spread=0.25`,
130→141 at `min_spread=0.10`, evidence the boundary is noisy at the margin even though the ballpark
is stable). For each quotable market, `close_time - now`:

| | `min_spread=0.25, vol≥100` (n=76) | `min_spread=0.10, vol≥100` (n=141) |
|---|---|---|
| already past `close_time` (pending resolution) | 7 (9.2%) | 12 (8.5%) |
| closes within 7 / 30 / 60 / 90 days (cumulative, beyond the above) | +0 / +0 / +0 / +0 | +0 / +0 / +2 / +2 |
| closes within 180 days (cumulative) | 76 (100%) | 140 (99.3%) |
| median days to close | ≈110.5 | ≈110.5 |
| longest-dated market | ≈110.6 days | ≈475.5 days (~1.3y) |

**Today's quotable cohort is bimodal, not smoothly spread out.** A small group (7–14 markets,
~9% of the pool) is already past its nominal close and could resolve at any time; essentially all
the rest — 69 of 76 at `min_spread=0.25`, ~127 of 141 at `min_spread=0.10` — cluster on almost
exactly the same date, **~110 days from the measurement, i.e. 2026-12-31**, evidently a large batch
of "will X happen in 2026" markets sharing one year-end close. There is a hard gap: zero markets
in either set close between roughly day 8 and day 109. **A market cannot resolve before its close
date** — so absent any new market entering the pool, this specific cohort would supply almost no
newly-resolved ("trading") markets between now and around January 2027.

### 5.2 What the answer therefore depends on, and what could not be measured today

Reaching "trading markets" over calendar time requires new markets to keep entering the quotable
pool as old ones resolve and leave it. **The rate at which that happens — new-market creation and
entry into quotability — cannot be measured from a single day's snapshot**; it requires repeated
snapshots over time, which is exactly the forward collection this report is asking whether to fund.
Two bounding scenarios, stated as assumptions rather than facts:

- **Scenario A (fastest defensible; optimistic).** Assume the entire quotable cohort turns over
  completely and independently every ~110 days — a same-sized, entirely new set of markets and
  events replaces the current one, with zero event overlap between cohorts. This is generous: many
  Polymarket categories (elections, rate decisions, macro thresholds) recur under the same or
  closely related events year over year, so "zero overlap" almost certainly overstates the rate at
  which *new, distinct* events appear.
  - `min_spread=0.25` (pool 75, events 32/cohort): 1,000 markets needs ⌈1000/75⌉ = 14 cohorts →
    **14 × 110 ≈ 1,540 days ≈ 4.2 years.**
  - `min_spread=0.10` (pool ~135, events ~53/cohort): 1,000 markets needs ⌈1000/135⌉ = 8 cohorts →
    **8 × 110 ≈ 880 days ≈ 2.4 years.**
  - At both thresholds the `≥30 events` floor clears inside the FIRST cohort (~110 days) under this
    scenario — event count is not what takes years; market count is.
- **Scenario B (more realistic; not bounded above).** New markets more plausibly enter as a
  continuous trickle of shorter-dated listings (daily crypto thresholds, weekly sports) rather than
  as one simultaneous big-bang replacement of the whole cohort, and the same handful of recurring
  event families (elections, rate decisions, annual thresholds) may re-appear release over release
  rather than being replaced by genuinely new ones. Under this scenario Scenario A's rate is an
  upper bound on speed, not a typical one — the true time to 1,000 markets could be substantially
  longer than 4.2 / 2.4 years, and the distinct-event count could plateau well under any number
  market count reaches, since GUARDRAILS.md's own warning applies here directly: *a thousand markets
  across twelve events is worth twelve markets.*

### 5.3 The answer

**At `min_spread=0.25`: never, in the words the brief asked for.** The fastest scenario this
snapshot can defend is ~4.2 years to reach 1,000 trading markets across ≥30 events; the more
realistic scenario is slower still, or indefinite if event diversity plateaus. No collection budget
this kit has operated under (weeks to a few months) reaches that.

**At `min_spread=0.10`: also years, not weeks — ~2.4 years under the same optimistic assumption,**
worse under the realistic one. Faster than `min_spread=0.25` by roughly 40%, but the same order of
magnitude, and the same "not within a normal project timeframe" conclusion applies. This report
takes no position on which of the two thresholds the user should run collection at; both numbers
are given side by side, as instructed, precisely because `min_spread=0.25` is not yet certified by
this kit's own review and a reader needs both to decide.

**The single largest unmeasured unknown is the new-market entry rate**, not anything a public GET
today can supply. If collection is ever run, a short (1–2 week) pilot aimed specifically at
measuring how many NEW markets enter the quotable set per day — rather than a multi-month
commitment made on today's single snapshot — would replace Scenario A/B's assumption with a
measured number before any larger decision is made.

---

## 6. What is structural and what is weather

**Structural (would not change on a different measurement day, or changes slowly over months):**
- Polymarket's quotable universe is roughly an order of magnitude smaller than Kalshi's, at every
  spread threshold measured (§1).
- Polymarket's quotable markets bundle more candidates per event than Kalshi's (§2) — its event
  count is the binding constraint on any future power calculation, not raw market count.
- The rebate is published via a per-market `feeSchedule.rebateRate` object, parsed to
  `source="venue_schedule"`, on the overwhelming majority of quotable markets at either threshold
  (§3) — this is how the venue's fee API is shaped, not a one-day fluctuation.
- Gamma's `/markets` listing enforces a hard offset ceiling around 2,000 raw items (§1) — a venue
  API limit, not a bug in this repo's adapter.

**Weather (specific to 2026-09-12, will differ on another day):**
- The exact quotable counts (75/130 at the two headline cells) and the 37-minute drift observed
  between two same-day snapshots (75→76, 130→141) (§1, §5.1).
- The exact `rate`/`rebateRate` value mix (0.03/0.04/0.05/0.07; 20%/25%) (§3) — Polymarket can and
  does change per-market fee schedules.
- The 48.7%/68.0% dwell-time change rates (§4) — a quieter or more volatile 15-minute window on
  another day would move these.
- **The Dec-31-2026 close-time cluster (§5.1) is calendar-specific and will not recur in the same
  form** — it reflects markets that happen to expire at THIS particular year-end, not a permanent
  feature of the venue. A measurement taken in, say, November 2026 or March 2027 would very likely
  see a different close-time distribution entirely. The *existence* of clustering (markets bunching
  around salient calendar dates) is plausibly structural; the *specific date* is not.

---

## Appendix: raw data

- Grid + fee distribution: `probe_grid_fees.py` output, `grid_fees_output.json` (scratchpad).
- Dwell time: `probe_dwell.py` (raw data collection) + `summarize_dwell.py` (the probe script's
  own built-in summary crashed on a `zip(..., strict=True)` bug after successfully writing all 16
  polls' raw data — noted here rather than silently fixed and re-run, since the raw data it was
  crashing on is exactly what was analyzed instead), `dwell_raw.json` / `dwell_summary.json`.
- Close-time distribution: `probe_close_times.py`, `close_time_output.json`.
- All four scripts are read-only (`list_markets`/`get_book` only) and live in the task scratchpad,
  not under `app/`.
