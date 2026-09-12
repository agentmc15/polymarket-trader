# Kalshi Gate 1 — full-power retrospective proof-out

**Data window.** Venue `kalshi`, hourly (`interval_minutes=60`) candles, 10-day lookback
per market. Sampled settled-market closes span **2026-07-03T07:03:47Z -> 2026-09-07T11:25:53Z**.
`n_markets` (quoted-or-not, replayed) = **15,283**, `n_candles` = 951,788. `n_quoted` = 9,931
under both fill models. `n_trading` (pessimistic, `overall`) = **4,320**; (pessimistic, `test`)
= **1,239**. Temporal cutoff = **2026-09-05T19:40:26Z**, chosen (not the harness's median-close
default) to put **69.99% of sampled closes in train**, per this task's brief. Policy:
`min_spread=0.10 edge_fraction=0.80 max_inventory=20.0 skew_strength=1.0 quote_size=10.0`.
Fees: `KalshiFeeModel maker_rate=0.0175 taker_rate=0.07 maker_rebate_rate=0.0
source=settings_default`.

## How this run was produced

This number did not come out of one invocation, and a reader of a GO/NO-GO document should see
the attempts behind it.

1. **`--sample 8000`** (the brief's starting point) collected 6,252 markets and ran cleanly, but
   was underpowered against acceptance: `n_trading` (pessimistic, overall) = **1,767** against a
   required 2,500.
2. **`--sample 20000`** crashed after ~27 minutes: an `httpx.ReadError` (a transport blip) inside
   the per-market candle fetch was not covered by `_collect_one`'s exception list
   (`VenueError, OSError, ValueError`), so it propagated out of the whole run. No cache was
   written — the harness flushed only at the end, so 27 minutes of live collection was discarded.
3. **`--sample 19000`** (retried smaller, to shorten exposure) crashed the same way, confirming
   this was a reproducible defect, not a one-off network blip.
4. A first harness fix landed: the per-market catch widened to
   `(VenueError, OSError, ValueError, httpx.HTTPError)`, and the candle cache began flushing every
   500 markets. **`--sample 20000`** was retried and crashed again — this time after ~80 minutes,
   in a *different, previously unprotected* code path: `settled_universe`'s `/events` listing walk
   (~150 pages) had no transport-error handling at all, and a dropped read there discarded the
   whole walk before candle collection ever began (no cache, because collection had not started).
5. A retry of the same command got past the listing walk on its own, but was killed deliberately
   once the second defect above was diagnosed, rather than let it risk hitting the same
   unprotected path again.
6. A second harness fix landed: a bounded retry (3 attempts, `httpx.HTTPError` only) around the
   listing walk, plus an on-disk universe cache keyed on `min_volume`. **`--sample 20000`** was
   run a final time and completed end to end. Its own log shows the new protections firing for
   real, not just in a test: `settled_universe: list_markets attempt 1/3 failed
   (ReadTimeout('')); retrying` (the bounded retry absorbing exactly the fault class that killed
   attempt 4), and 11 per-market `request` errors landed in `collection.failure_sample` as counted
   failures rather than a crashed process (evidence the widened per-market catch works). This run
   walked the settled universe **fresh, immediately before sampling** (its own stdout says
   `universe: walked`, not "loaded from cache" — no pre-existing universe cache existed on this
   machine before this run), then spent from **06:12:40 to 09:00:48** (local, ~2h48m) collecting
   candles for the sampled markets — several times longer than the ~25-minutes-for-8,000
   estimate in the brief scaled to 20,000 would predict (~40-60 min), which this report states as
   measured rather than assumed. `backend/.cache/mm/kalshi-60m.universe.json` is a resume artifact
   for a *future* invocation; it was not reused by this one.
7. None of runs 1-6 had passed `--temporal-cutoff` yet, so all of them scored on the harness's
   *default* median-close (~50/50) split rather than the brief's specified ~70%-train split. A
   50/50 read of run 6's cache showed a pessimistic test CI of **[+0.0807, +0.5415]**, entirely
   above zero, i.e. it looked like a GO. That split does not satisfy the brief. Applying the
   brief's actual instruction — a
   `--temporal-cutoff` chosen so ~70% of the *same, already-cached* markets close in train (a
   same-day, offline, seconds-long recomputation against the existing candle cache; no new
   network activity) — moved the cutoff later, shifted lower-trading-rate, more-recent closes
   into the test split, and **reversed the verdict to NO-GO** (test CI **[-0.1205, +0.4507]**).
   Both readings are reported below so neither is mistaken for the other, but **the verdict in
   this report is computed on the correctly-specified 70%-train split**, because that is what the
   brief and PLAN.md D5/GUARDRAILS §4.1 require for a go/no-go, and a report that scored on the
   more convenient split without saying so would be exactly the kind of "check that cannot fail"
   this kit's own history (`NOTES.md`) has repeatedly had to catch.

Net: **6 collection attempts, 4 of them consumed by two distinct unprotected transport paths in
the harness** (now fixed), plus one additional offline re-score to apply the specified split.

## Universe and survivorship

`n_universe` (settled, `volume >= 2000`) = **50,470**. `excluded_by_result` (settled to something
other than `yes`/`no`) = **168** (`survivorship_share` = 0.33%) — small and not the caveat that
matters here.

**`collection.universe_provenance`, copied in full:**

```json
{
  "listing_is_page_capped": true,
  "cap": "_MAX_EVENT_PAGES=150 pages x _EVENTS_PAGE_LIMIT=200 events/page in app/venues/kalshi/adapter.py; the listing did not exhaust at 400 pages / 1,241,961 markets when walked past the cap",
  "sample_is_random_draw_from_venue": false,
  "measured_on": "2026-09-07",
  "visible_tradeable_markets": 50826,
  "invisible_tradeable_markets": 83151,
  "visible_share_of_tradeable_universe": 0.3793636221142435,
  "bias": "month-correlated, not random. Tradeable settled markets inside the cap vs beyond it: 2026-07 2,125/480; 2026-08 4,613/77,602 (94% missing); 2026-09 44,087/5,069. The listing is not chronologically ordered, so this is not a missing recent tail.",
  "consequence": "n_universe counts the VISIBLE settled listing, so n is not a random draw from the venue and a verdict computed on it generalises to 'markets like the ones the listing shows', not to Kalshi. Distinct from survivorship_share, which counts only exclusion by a non-binary result. A temporal split within the visible set remains internally valid."
}
```

**In this report's own words:** the Kalshi settled-market listing this run drew from shows only
**37.9%** of the tradeable settled universe (50,826 of 133,977) — not because of anything this
run did, but because `list_markets(status="resolved")` stops at a 150-page cap that the real
listing exceeds by more than 8x when walked past it. The missing 83,151 markets are not a random
draw's complement: **August 2026 is 94% missing** (4,613 visible of 77,602+4,613), while July and
September are mostly visible. So this report's verdict does **not** generalise to "Kalshi's
settled history" — it generalises to **"markets like the ones the page-capped listing happens to
show"**, which skews away from whatever made August's markets take longer to settle or get
indexed later. The temporal split *within* the visible set (train vs test by close date) remains
internally valid regardless — that comparison never needed the full universe, only the sample.

**Short-history exclusion, and whether it skews the visible window.** `n_requested` = 20,000;
`n_collected` = 15,283; **`n_short_history` = 4,706 (23.53% of requested)** — markets with fewer
than 4 hourly candles in the 10-day lookback, i.e. barely traded before close. `payload_errors` =
0; `request_errors` = 11 (0.055% of requested — see below); `failure_rate` = 0.00055, far under
the harness's 2% abort threshold. The harness does not retain a market id or close date for a
market it drops as too-short (`CollectResult` counts it, by design, as "not a failure" — see its
docstring), so this run cannot identify *which* markets were dropped and check their close dates
directly. As the closest available proxy, the close-date composition of the markets that
*survived* the filter closely tracks the provenance table's own visible-universe proportions by
month (survived: 2026-07 4.57% / 2026-08 11.33% / 2026-09 84.10%, versus the visible universe's
2026-07 4.18% / 2026-08 9.08% / 2026-09 86.74%) — August is slightly *over*-represented and
September slightly *under*-represented among survivors relative to the visible universe, which is
the opposite of what a "short-history exclusion silently erases the already-thin August window"
concern would predict, though a single 20,000-market draw cannot rule out sampling noise as the
explanation. This is stated as a measured limit, not a settled fact: the harness's design (correctly, for its purpose — see its docstring) does not let this report say more.

## Results — pessimistic first (GUARDRAILS §2.2), cash P&L (`pnl_basis=cash_settled`)

All figures below: `fill_model` as labelled, `terminal=settled`. `pnl` is cash — settled fills
plus terminal inventory settled at the venue result, no mark anywhere in it. `markout_pnl` is the
mark-dependent quote-quality statistic (marked once at candle `i+2`'s mid), reported beside it and
never the basis of the CI, ROC, or verdict.

### fill_model=pessimistic, terminal=settled

| block   | n_quoted | n_trading | n_events_trading | mean pnl/mkt | sd     | 95% CI (event-clustered) | mean markout | roc    | held | short→yes |
|---------|---------:|----------:|------------------:|-------------:|-------:|:-------------------------|-------------:|-------:|-----:|----------:|
| overall |    9,931 |     4,320 |              2,479 |      +0.2913 | 4.5241 | [+0.1622, +0.4314]       |      +0.2369 | 0.0195 | 3,390|     1,414 |
| train   |    6,763 |     3,066 |              1,758 |      +0.3490 | 4.5599 | [+0.1785, +0.5320]       |      +0.2570 | 0.0245 | 2,411|     1,004 |
| test    |    3,127 |     1,239 |                717 |      +0.1501 | 4.4176 | [-0.1205, +0.4507]       |      +0.1888 | 0.0090 |   967|       406 |

Total cash pnl (`fill_model=pessimistic, terminal=settled`): overall +$1,258.49, train +$1,069.91,
test +$185.92.

### fill_model=optimistic, terminal=settled

| block   | n_quoted | n_trading | n_events_trading | mean pnl/mkt | sd     | 95% CI (event-clustered) | mean markout | roc    | held | short→yes |
|---------|---------:|----------:|------------------:|-------------:|-------:|:-------------------------|-------------:|-------:|-----:|----------:|
| overall |    9,931 |     4,452 |              2,549 |      +0.3831 | 4.6341 | [+0.2143, +0.5484]       |      +0.3355 | 0.0264 | 3,490|     1,447 |
| train   |    6,763 |     3,154 |              1,805 |      +0.4627 | 4.6732 | [+0.2673, +0.6314]       |      +0.3815 | 0.0334 | 2,464|     1,022 |
| test    |    3,127 |     1,283 |                741 |      +0.1868 | 4.5107 | [-0.0959, +0.4928]       |      +0.2199 | 0.0117 | 1,013|       421 |

**Both fill models agree on the shape of the result** (`terminal=settled` both): the *overall*
CI is entirely positive under both pessimistic ([+0.1622, +0.4314]) and optimistic
([+0.2143, +0.5484]); the *out-of-sample test* CI spans zero under both pessimistic
([-0.1205, +0.4507]) and optimistic ([-0.0959, +0.4928]). The finding is not an artifact of the
conservative queue assumption — even the generous model does not clear zero out of sample here.

`rebate_if_paid_not_in_pnl` (`fill_model=pessimistic, terminal=settled`, GUARDRAILS §2.3): **$0.00
would add if paid as published** — not included in any P&L above. Kalshi's schedule used here
(`source=settings_default`) carries `maker_rebate_rate=0.0`, so this line is trivially zero on
this venue's current published terms; it is reported anyway per §2.3's rule, not omitted because
it happens to be zero.

## In-sample vs out-of-sample: neither should be mistaken for the other

The *overall* block (`fill_model=pessimistic, terminal=settled`) scores every replayed market,
train and test together, and its CI [+0.1622, +0.4314] is entirely above zero. That is real
evidence that a positive effect exists **somewhere in this sample** — it is the first time any
measurement in this project's history has produced an all-positive CI at this scale. It is **not**
an out-of-sample statement: parameters were not tuned on a split here, but the overall block still
mixes in the very data whose later half the temporal test exists to hold out.

The *test* block — later closes only, temporal split, no train-side event sharing a market with it
— is the one PLAN.md D5 and this brief's verdict rule are computed on: **[-0.1205, +0.4507]**,
spanning zero. So the honest statement is: **there is a positive effect in this sample as a
whole, and it does not survive being tested on data the model did not see.** That gap is exactly
what an out-of-sample split exists to catch, and reporting only the overall number would have
hidden it.

**Cutoff sensitivity, stated because it changed the answer.** The harness's *default* temporal
cutoff (median close, ~50% train) scored on this same cache gives a pessimistic test CI of
**[+0.0807, +0.5415]** — a GO by the same rule. The brief's specified **~70%-train** cutoff, which
this report uses, gives **[-0.1205, +0.4507]** — a NO-GO. Moving the boundary later shifts more
of the (generally higher-trading-rate) earlier closes into train and leaves the test split with a
smaller, more-recent, lower-trading-rate slice — visible directly in `n_trading`: 1,939 at the
50/50 default vs 1,239 at 69.99% train, with `n_events_trading` falling from 1,179 to 717 in
step. Both are legitimate splits of the same data; only the second is the one the brief specifies
for a verdict, and it is the one this report's `VERDICT` line is computed on.

## n_unmarkable_intervals

`n_unmarkable_intervals` (`fill_model=pessimistic, terminal=settled`) = **0** of **921,222**
quote/fill/mark triples examined (identical under `fill_model=optimistic`). No interval was
discarded for lacking a two-sided book to mark against at `i+2`. This settles a carry-forward from
the T2 verifier, who measured 0 of 1,552 (verify sample) and 0 of 11,636 (a 224-market live
sample) and flagged that T3's ~8,000-market run, and especially the harness's first full-power
pass, was the first place a nonzero rate could plausibly appear. At **921,222 intervals** — 79x
the earlier live sample — it is still exactly zero. The cash figure this report's verdict rests on
is computed on the **full** replayed sample, not a filtered subsample.

## Event concentration

`n_events_trading` is reported alongside `n_trading` in every table above. Concentration is low
and not a threat to the clustered CI's credibility:

- Overall (`fill_model=pessimistic, terminal=settled`): 4,320 trading markets across 2,479
  events (1.74 markets/event). Largest event: `KXVANCEMENTION-26SEP03` with 14 markets — **0.32%**
  of the trading sample.
- Test split (the one the verdict is computed on): 1,239 trading markets across 717 events (1.73
  markets/event). Largest event: `KXNASCARTOP10-COOOS26` with 12 markets — **0.97%** of the test
  trading sample.

For contrast, the earlier 8,000-market run's universe carried single events of up to 155 markets
(a golf bracket) and 130 markets (a halftime prop family) — double-digit percentage shares of a
much smaller trading sample. Nothing in this run's trading sample approaches that: **the largest
single event never exceeds 1% of any block**, so the event-clustered CI here reflects on the order
of hundreds to low thousands of independent real-world outcomes, not a handful of correlated
families repeated many times. The power table's `pool_n_events` (below) is well clear of
`MIN_POWER_POOL_EVENTS=30` in every block for exactly this reason.

## Power (test split, resampling whole events with replacement, cash P&L)

### fill_model=pessimistic, terminal=settled — pool 1,239 markets in 717 events (floor 30 events, not insufficient)

| portfolio (markets) | events drawn | 5th pct total pnl | P(profit) |
|---:|---:|---:|---:|
| 500   | 289   | -117.62 | 0.7580 |
| 1,000 | 579   | -100.14 | 0.8540 |
| 2,500 | 1,447 |  -77.23 | 0.9200 |
| 5,000 | 2,893 | +139.37 | 0.9820 |

### fill_model=optimistic, terminal=settled — pool 1,283 markets in 741 events

| portfolio (markets) | events drawn | 5th pct total pnl | P(profit) |
|---:|---:|---:|---:|
| 500   | 289   |  -96.78 | 0.7880 |
| 1,000 | 578   |  -61.09 | 0.9000 |
| 2,500 | 1,444 |  +36.17 | 0.9560 |
| 5,000 | 2,888 | +284.96 | 0.9980 |

Reading this beside the verdict: even though the mean P&L per trading market is positive
(pessimistic +0.2913 overall / +0.1501 test), a portfolio has to reach roughly **5,000
simultaneous markets** before its pessimistic 5th-percentile outcome turns positive — below that,
a portfolio built at this policy's parameters has a real chance of a losing period even if the
long-run mean holds. This is consistent with, not contradicted by, the test CI spanning zero: a
positive mean with wide per-market variance and a CI that includes zero is exactly what a power
table like this looks like.

## Split exclusions

`split_exclusions` (`fill_model=pessimistic, terminal=settled`, cutoff
`2026-09-05T19:40:26+00:00`, 69.99% of sampled closes in train): **19 events straddle the cutoff**
(have markets on both sides); **75 post-cutoff markets were dropped from `test`** so no test
market shares an event with a train market. Those 75 markets remain in `overall`. (Optimistic:
identical straddling/drop counts — the split is on `close_ts`/`event`, independent of fill
model.)

## Request errors and rate limiting

**429 rate: 0%** across every attempt in this run's history (zero `kalshi_rate_limited_retrying`
log lines in any of the six collection attempts) — well under the 1% threshold that would require
halving concurrency, so concurrency stayed at 8 throughout and pacing was never disabled.

**Request errors: 11** (`collection.n_request_errors`, 0.055% of the 20,000 requested) — the
first live evidence, not just a passing test, that the harness's widened per-market exception
catch (`VenueError, OSError, ValueError, httpx.HTTPError`) works: each of these 11 markets hit a
transient transport fault, was counted, and was skipped, rather than terminating the run the way
it did in attempts 2 and 3. `payload_errors: 0` — nothing indicates the venue's payload shape
moved under T1's parser.

## Verdict

GO iff `ci_low > 0` on the pessimistic, cash, temporal-test split. It is not:

```
VERDICT kalshi fill_model=pessimistic terminal=settled split=temporal-test ci_low=-0.1205 ci_high=+0.4507 n_trading=1239 -> NO-GO
```

**This is a NO-GO on a retrospective replay under stated fill-model assumptions, computed on the
Kalshi listing's visible (page-capped) settled markets — it does not generalise to "Kalshi," it
generalises to "markets like the ones the listing shows" (see `universe_provenance` above).** The
sample is not underpowered (`n_trading=4,320` overall, `1,239` on the scored test split, both well
past this report's acceptance floors, and the power table's event pool clears `MIN_POWER_POOL_EVENTS=30`
by more than 20x) — this is a clean negative, not an inconclusive one. The in-sample (`overall`)
CI is positive; the out-of-sample (`test`) CI is not, and the verdict is computed on the latter
because that is what a go/no-go requires. This finding is Gate 1's input to T13's cross-venue
go/no-go writeup; it is not a decision to trade, and nothing in this report or its underlying code
places, modifies, or cancels an order.
