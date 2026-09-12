# Gate 2 — the smallest real-money test that answers what paper cannot

**This kit built no order-placing code, and this document adds none. There is no
order placement, no cancel, no replace, no "dry-run" order path and no test
fixture that could emit one anywhere in `mm-proveout`; every module it produced
is read-only, `backend/tests/test_fences.py` enforces that by AST walk over the
whole `app/` tree, and this task added no file to its allowlist. What follows is
a specification for a FUTURE kit. Nothing in it is authorized, scheduled, or
recommended.**

**This document does not recommend trading real money, at any size, on either
venue.** It specifies a test, states the entry condition that would have to hold
before the test could be run, and stops. Whether to run it is the user's
decision and is out of this kit's scope by construction.

Per `mm-proveout/GUARDRAILS.md` §2.1, every P&L, ROC and spread figure below
carries `fill_model=` and `terminal=`; per §2.2 pessimistic is stated first and
every threshold is set on it; per §2.3 the rebate is its own line and is inside
no P&L figure; per §2.4 every interval is clustered by event. Figures derived by
arithmetic from cited fields are labelled **derived** and the arithmetic is
shown, so it can be checked rather than believed.

---

## 0. Scope: neither venue qualifies today, and this document says so rather than manufacturing a subject

The brief for this task scopes the design to "whichever venue(s) T13 marked GO or
UNDERPOWERED-but-close". Read against `reports/go-no-go.md` §7, **neither venue
is GO and neither is UNDERPOWERED-but-close**:

| venue | T13 verdict | is it "close"? |
|---|---|---|
| Kalshi | **NO-GO** | No. NO-GO is not a flavour of UNDERPOWERED. Four of five pre-committed conditions were met, but the one that was met on the statistics — the CI clearing zero — fails on removal of any one of three series, sits with its lower bound at 8.37% of its mean, and is one of 33 event-clustered CI evaluations of a single test split (`go-no-go.md` §3.3). |
| Polymarket | **UNDERPOWERED** | No. Zero forward snapshots exist, collection has never started, and reaching this kit's own power floor is ~2.4 years at `min_spread >= 0.10` and ~4.2 years at `>= 0.25` under an explicitly optimistic scenario (`go-no-go.md` §4.4; `polymarket-feasibility.md` §5.3). That is the opposite of close. |

So this document is written in the conditional. **It is the test that would be
run if and when a venue qualifies**, with the entry condition stated explicitly
in §8 and not assumed away anywhere else.

### 0.1 Why the document is still worth writing, and why that is not an argument for running it

One thing in this kit is not a power problem and will not be fixed by more
replay at any `n`. **Queue position is the whole thesis, and no paper study can
observe it.** `app/strategies/market_making.py`'s own header states the
measurement, on 34,137 hourly candles across 537 settled Kalshi markets
(`fill_model` given per row, `terminal=settled`):

      fill model         fills   quoted half   realized half   adverse
      front of queue     7,857     +0.0182       +0.0051        72%
      behind the queue   3,046     +0.0236       -0.0095       140%

That is a sign flip between two assumptions about the same books. Every number
this kit has produced sits between those two rows, and `pessimistic` — the
honest default for a newcomer, and the branch every verdict here is computed on —
is **still a model of queue position, not an observation of one** (`go-no-go.md`
§5.1). Real resting orders are the only instrument that resolves it.

**That is a reason the question survives a NO-GO. It is not a reason to place an
order.** §8 states the entry condition; this section does not weaken it.

---

## 1. Purpose — one question, one primary statistic, two paired observables

**The question.** Where does a participant with no standing on the venue
actually sit in the queue, and is `pessimistic` pessimistic enough?

**The primary statistic** (the one the verdict is computed on):

> **realized per-contract markout half-spread** = `total_markout_pnl / n_fills /
> quote_size` over live maker fills, where each fill is marked once at
> `mid(i+2)` — the same `mark_to_market(fill, mid(i+2))` convention
> `app/execution/passive_fill.py` and `app/scripts/mm_backtest.py` already use,
> and the same `markout_basis=marked_at_i_plus_2` label every markout figure in
> this kit carries.

**The companion observable** (which says *which fill model* the realized world
matched, and is the thing the primary statistic cannot say on its own):

> **realized fill rate** = fills taken per resting quote-interval, i.e.
> `n_fills / quote_hours` on the live session's own clock.

The two are paired on purpose. A newcomer behind the queue is hurt twice — it
fills less often, and the fills it does get are the adverse ones. A test that
measured only the half-spread could not distinguish "we sat behind the queue"
from "the venue was quiet"; a test that measured only the fill rate could not
distinguish "we filled plenty" from "we got picked off every time".

### 1.1 The predictions the test scores against

Both fill models make a prediction for both observables. **Pessimistic first
(GUARDRAILS §2.2).** These come from the 1-minute honest holdout, because a live
quoter experiences 1-minute resolution and the kit has measured that resolution
**changes the sign** of the result, not just its size (`market_making.py` header,
finding 2: the old defaults measure **+0.2038** mean P&L/mkt hourly and
**−0.2258** at 1-minute on the same 7,985 markets).

Source for every field: `reports/kalshi-honest-holdout.json →
policies.candidate(0.90/0.25/50.0).{pessimistic,optimistic}.test`,
`split=temporal-test`, `terminal=settled`, `markout_basis=marked_at_i_plus_2`,
policy = shipped defaults `edge_fraction=0.90, min_spread=0.25,
max_inventory=50.0, quote_size=10.0, skew_strength=1.0`
(`kalshi-density-gate.json → provenance.policy`).

| observable | `fill_model=pessimistic` | `fill_model=optimistic` |
|---|---:|---:|
| `n_fills` / `quote_hours` (cited) | 2,392 / 163,219 | 2,545 / 163,219 |
| **predicted fill rate per resting quote-minute** (derived) | **1.4655%** | **1.5593%** |
| `total_markout_pnl` / `n_fills` (cited / cited) | 577.22 / 2,392 | 662.05 / 2,545 |
| markout per fill (derived) | $0.241313 | $0.260138 |
| **predicted per-contract markout half-spread** at `quote_size=10.0` (derived, ÷10) | **+$0.024131** | **+$0.026014** |
| `mean_pnl_per_trading_market` (cited, `pnl_basis=cash_settled`) | +$0.43598 | +$0.45299 |
| `n_trading` / `n_quoted` (cited) | 919 / 1,581 | 935 / 1,581 |

**The two 1-minute predictions are close together and both positive.** That is
itself worth stating: at 1-minute resolution on this sample the two queue
assumptions disagree by 8.4% on markout and 6.4% on fill rate — nothing like the
sign flip the hourly bucket study showed. A live realized number **below
+$0.024131 per contract** is therefore not "somewhere between the models"; it is
**outside the bracket both models predict**, and it would mean `pessimistic` is
not pessimistic enough and every verdict in this kit computed on it is
optimistic.

### 1.2 The hourly bucket prediction, shown and deliberately NOT used as the threshold

`market_making.py`'s bucket table gives realized half-spread per fill by quoted
spread, 95% CI clustered by event, on the original hourly 537-market
calibration. For the `>= 0.25` bucket the shipped policy quotes in:

| bucket | front of queue (optimistic) | behind the queue (pessimistic) |
|---|---|---|
| `>= 0.25` | +0.1152 `[+0.0867, +0.1465]` | +0.0735 `[+0.0270, +0.1225]` |

**Those numbers are 3.0x the 1-minute prediction in §1.1 (+0.0735 against
+0.024131), and the kit has already explained why**: hourly candles average away
the intra-hour adverse selection a live quoter actually eats
(`market_making.py`, "THE MECHANISM"). They are shown here so a future kit does
not rediscover them and mistake them for the threshold. **The pass rule in §5
uses the 1-minute numbers.** Using the hourly bucket would set a bar the
1-minute evidence says nothing can clear.

Two further limits on the bucket table, both load-bearing: it is **in-sample**,
and it was measured under the **old** `edge_fraction=0.80 / min_spread=0.10 /
max_inventory=20.0` calibration, not the shipped `0.90/0.25/50.0`.

### 1.3 What this test is NOT for

It is **not** a re-measurement of P&L. The expected cash P&L of the whole test,
computed below in §4.4, is **about +$3**. It is an instrument, not a trade. Any
design pressure to make it "worth doing financially" is pressure to widen it, and
widening it is exactly what GUARDRAILS §2.6 forbids.

---

## 2. Venue, policy and size

### 2.1 Venue: Kalshi, for the queue arm

Kalshi is the only venue where the replay has produced predictions to test
against. **Polymarket has no P&L, no ROC, no CI, no power table, no collateral
figure and no order rate in any report in this kit** (`go-no-go.md` §4.4), so
there is nothing for a Polymarket queue test to score `realized >= predicted`
against. A Polymarket queue arm cannot be specified until forward snapshots
exist and `mm_replay_snapshots.py --markout-only` has produced a prediction from
them.

The Polymarket arm in §7 is a different and much smaller instrument: it answers
a binary question about the rebate, needs ~10 fills rather than 100, and needs
no prediction from a replay.

### 2.2 Policy: the shipped defaults, with `max_inventory` rescaled — and why the rescale is mandatory

| parameter | value | why |
|---|---|---|
| `edge_fraction` | **0.90** | shipped default; the value the predictions in §1.1 were computed at |
| `min_spread` | **0.25** | shipped default; the only bucket positive under BOTH fill models (`market_making.py`) |
| `skew_strength` | **1.0** | shipped default, unchanged |
| `taper_hours` | **0.0** | shipped default — the taper is disabled, and `kalshi-taper.md` measured it as a monotonic cost: test-split pessimistic `ci_low` fell at every step from `+0.4631` (0h) to `+0.0341` (24h), `fill_model=pessimistic`, `terminal=settled` |
| `quote_size` | **1.0 contract** | the venue minimum — see §2.3 |
| `max_inventory` | **5.0 contracts** | **rescaled from 50.0.** Not optional; see below. |

**Why `max_inventory` must be rescaled from 50.0 to 5.0.** `MarketMaker.quote()`
uses inventory only through the ratio `inventory / effective_max_inventory`
(`market_making.py:679-683` computes `clamped = max(-1.0, min(1.0, inventory /
effective_max_inventory))`, and `:695` withdraws a side at `inventory >=
effective_max_inventory`). Inventory accumulates in units of `quote_size`. So the
policy's behaviour is invariant under proportional scaling of the pair
`(quote_size, max_inventory)` and **only** under that scaling:

- quoted **prices** depend on `mid`, `spread`, `edge_fraction` and `lean`, and
  `lean` depends on the invariant ratio → prices are bit-identical;
- the withdrawal point is the same fraction of the way to the cap;
- therefore the **fill set is exactly identical**, and the §1.1 fill-rate
  prediction transfers to `quote_size=1.0` unchanged.

The replay ran `quote_size=10.0, max_inventory=50.0` — a ratio of 5. Keeping
`max_inventory=50.0` at `quote_size=1.0` would make it 50, an entirely different
risk posture (50 one-sided fills before withdrawal instead of 5), and the §1.1
predictions would no longer describe the policy being run. **5.0 is the value
that preserves the geometry the predictions were measured under.**

### 2.3 `quote_size` = 1 contract, and the fee trap that comes with it

Kalshi's minimum order size in this repo is **1 contract**
(`app/venues/kalshi/adapter.py:239-242`: "Kalshi trades whole contracts and
PLAN.md §3 pins no per-market minimum field, so 1 contract is the floor unless a
payload states otherwise"). **That is a documented default, not a live
measurement** — a prerequisite probe (§8.2) must confirm it against live market
payloads before any sizing depends on it.

**THE TRAP, and it is first-order.** `KalshiFeeModel` rounds the per-fill maker
fee UP to the nearest $0.000001 and then, when `round_net_to_cents=True` (the
default, modelling the $0.01 net floor for non-direct members), **up again to the
nearest whole cent** (`app/venues/fees.py:288-315`). At `quote_size=1` the cent
ceiling swallows the formula entirely. Computed by calling the repo's own fee
model with `FeeSchedule(taker_rate=0.07, maker_rate=0.0175,
source='settings_default')`:

| `quote_size` | p=0.10 | p=0.25 | p=0.50 |
|---|---:|---:|---:|
| 1 contract — fee per fill | $0.010000 | $0.010000 | $0.010000 |
| 1 contract — **fee per contract** | **$0.010000** | **$0.010000** | **$0.010000** |
| 10 contracts — fee per contract | $0.002000 | $0.004000 | $0.005000 |
| **increment at size 1** (derived) | **+$0.008** | **+$0.006** | **+$0.005** |
| as a share of the predicted +$0.024131 per-contract markout (derived) | **33.2%** | **24.9%** | **20.7%** |

At the venue minimum the maker fee is a **flat $0.01 per fill regardless of
price**, and it costs 21–33% of the entire predicted per-contract edge relative
to the size-10 run the prediction came from. The replay's `pnl` already subtracts
`fill.fee` (`mm_backtest.py:1827`), so **the §1.1 prediction embeds the size-10
fee and does not apply at size 1.**

**Consequence, and it is a hard prerequisite (§8.2): the prediction must be
re-derived at `quote_size=1.0, max_inventory=5.0` before the test starts.** That
is a re-run of an existing read-only script over an existing cache — no new code,
no order, no new venue traffic beyond the `list_markets` GETs the harness already
makes:

    cd backend && python3 -m app.scripts.mm_backtest \
      --cache .cache/mm/kalshi-honest-1m.json \
      --interval 1 --days 10 --seed 20260912 \
      --temporal-cutoff 2026-08-21 \
      --min-spread 0.25 --edge-fraction 0.90 --skew-strength 1.0 \
      --quote-size 1 --max-inventory 5 \
      --out <reports/gate2-prediction-size1.json>

The fill-rate prediction (1.4655% pessimistic) will come back **unchanged**, for
the reason in §2.2. The markout prediction will come back **lower**, and the
number it comes back with — not +$0.024131 — is the right-hand side of the pass
rule in §5.

A second prerequisite fact, not an assumption: whether the account is a **direct
member** (exempt from the $0.01 net floor, `round_net_to_cents=False`) or not.
The two regimes differ by up to 33% of the edge being measured.

---

## 3. Number of markets — the arithmetic to >= 100 fills

All rates from `kalshi-honest-holdout.json → policies.candidate(0.90/0.25/50.0).pessimistic.test`,
`fill_model=pessimistic`, `terminal=settled`, `split=temporal-test`.

**Step 1 — fills per quoted market.**

    n_fills / n_quoted = 2,392 / 1,581 = 1.512966 fills per quoted market   [derived]

**Step 2 — quoted markets needed for 100 fills.**

    100 / 1.512966 = 66.10  ->  67 quoted markets

**Step 3 — the quote rate.** Of the 4,569 markets in the test split the policy
looked at, 1,581 were ever quoted (the rest were declined — a one-sided book, a
crossed book, or a spread below 0.25, which `MarketMaker` records as a `reason`
rather than a gap):

    n_quoted / n_markets = 1,581 / 4,569 = 0.346028   [derived]

**Step 4 — markets that must be offered to the policy.**

    67 / 0.346028 = 193.63  ->  194   ->  **subscribe to 200 markets**

The rounding from 194 to 200 is 3.1% of headroom against the kit's own measured
projection error: T20 projected a 35% trading rate from T18 and realized 20%,
because the honest sample averaged 274 candles per market against T18's 613
(`kalshi-honest-holdout.md` §3.2). 3% is not enough headroom against a 43% miss,
which is why the acceptance in §5 is **>= 100 realized fills**, not "200 markets
were subscribed" — the test runs until the fills arrive or the duration cap in §4
stops it, whichever comes first.

**Cross-check, same arithmetic from the other end.** Quoted market-hours:
`163,219 / 60 = 2,720.32` (derived), giving `2,392 / 2,720.32 = 0.879309` fills
per quoted market-hour (derived). 100 fills therefore needs **113.73 quoted
market-hours** — and at the measured `163,219 / 1,581 / 60 = 1.7206` quoted hours
per quoted market (derived), that is 66.1 quoted markets. The same number, as it
must be.

**Of the 67 quoted markets, ~39 will take a fill.** `n_trading / n_quoted =
919 / 1,581 = 0.581278` (derived), and each trading market averages `2,392 / 919
= 2.602829` fills (derived). So the clustered CI in §5 is computed over roughly
39 trading markets across roughly 33 events (at the measured
`mean_markets_per_event = 1.164766`, `kalshi-honest-holdout.json → …power.500`).
**That clears this kit's own `>= 30 distinct events` floor by three events.** It
is a thin margin and §5.4 states what to do when the interval comes back too
wide to decide.

### 3.1 The concurrency cap, and why it is not 200

200 markets resting two-sided quotes on a 1-minute refresh is

    200 markets x 2 sides / 60 s = 6.667 orders/s   [derived]

against Kalshi's documented ~10 requests/second unauthenticated limit
(`mm-proveout/PLAN.md:83`; `app/venues/kalshi/adapter.py:341`; measured in the
market-edge kit as 17 requests in 1.69 s with a 429 on the 18th,
`market-edge/NOTES.md:6880`). **67% of the limit is not headroom.**

`go-no-go.md` §3.5 measured the replay's mean simultaneity at 6.60 markets
quoting at once and stated the limit of that number in its own words: *"The
headroom is in the average, not the peak … nothing here measures a peak, and no
report in this kit does."* That warning binds here. The replay's low simultaneity
is an artifact of 1,581 markets being spread across a 412-hour window at ~1.72
quoted hours each — it is **not** a prediction that 200 simultaneously-live
subscribed markets will quote one at a time.

**So the design caps concurrency rather than inferring it:**

> `MAX_CONCURRENT_QUOTED_MARKETS = 50` — a new setting the future kit must add.
> 50 x 2 / 60 = **1.667 orders/s**, 17% of the documented limit. Markets are
> admitted to the quoting set only as others close or are dropped; the 200-market
> requirement in Step 4 is satisfied over the test's life, not at any instant.

A read-only prerequisite (§8.2) can retire the 1-minute-refresh assumption
entirely: count, over the existing `kalshi-honest-1m.json` cache, how often the
rounded quote pair actually **changes** between consecutive intervals. Every
interval where it does not change needs no cancel/replace at all, and the real
order rate is that fraction of 1.667/s. That is pure arithmetic over a file
already on disk — no venue traffic, no order.

---

## 4. Capital cap, as a research cost — with the derivation

### 4.1 The starting figure: T2's measured collateral per market

`app/scripts/mm_backtest.py:1693-1706` defines collateral exactly:

    _collateral(pair) = bid.price * bid.size + (1 - ask.price) * ask.size

— a resting BUY ties up what it would pay; a resting SELL ties up what a short
contract pays out if YES resolves. Accrued per **quoted** interval only.

The measured value, `fill_model=pessimistic`, `terminal=settled`,
`split=temporal-test` (`kalshi-density-gate.json →
capital_picture.blocks.ungated.collateral_mean_usd_per_quoted_market`;
identically `kalshi-honest-holdout.json → …pessimistic.test.collateral_mean`):

    collateral_mean = $4.97041090362718 per quoted market, at quote_size = 10.0

**It is linear in size** (both terms multiply `size`), so:

    per contract:  $4.97041090362718 / 10 = $0.497041   [derived]
    at quote_size = 1.0:  **$0.497041 per quoted market**

*Sanity check on that number, because it is the load-bearing one.* A two-sided
quote ties up `size x (1 - quoted_spread)`, and the policy's quoted spread is
`edge_fraction x book_spread = 0.90 x book_spread`. So `$0.497041` per contract
implies a mean quoted spread of `1 - 0.497041 = 0.502959` and a mean **book**
spread of `0.502959 / 0.90 = 0.558843` across the intervals the policy quoted
(derived). That is consistent with a policy that only quotes when the book spread
is already `>= 0.25`, on a venue whose qualifying books are very wide — and it is
why collateral is about half of `size`, not the ~97% of `size` a tight book would
imply. 97.85% of quoted intervals were two-sided (`n_two_sided / quote_hours =
159,713 / 163,219`, derived), so the mean is not being dragged down by one-sided
quotes.

### 4.2 The hard ceiling, independent of the measured mean

A cap must be a ceiling, not an average. Collateral per contract on a two-sided
quote is `1 - 0.90 x book_spread`, which is **largest when the book spread is
smallest**, and the policy refuses to quote below `min_spread = 0.25`. So:

    max collateral per contract = 1 - 0.90 x 0.25 = **$0.775**   [derived, a hard bound]

At the §3.1 concurrency cap:

    50 markets x 1 contract x $0.775 = **$38.75**  instantaneous ceiling   [derived]
    (measured-mean expectation: 50 x $0.497041 = $24.85)                   [derived]

### 4.3 The cap as configured

Positions in markets that have filled but not yet settled sit outside the resting
collateral, so the open-notional cap must cover more than $38.75. Setting it at
the 200-market figure `200 x $0.775 = $155.00` (derived) gives the whole
subscription's worth of room and still caps the account four times below the
repo's shipped default of $1,000.

| setting | Gate 2 value | shipped default | derivation |
|---|---:|---:|---|
| `MAX_ORDER_NOTIONAL_USD` | **1** | 250 | one contract, price <= $1.00 |
| `MAX_OPEN_NOTIONAL_USD` | **155** | 1,000 | `200 x 1 x $0.775` (§4.2) |
| `MAX_DAILY_LOSS_USD` | **25** | 100 | §4.4 |
| `MAX_NEAR_RESOLUTION_NOTIONAL_USD` | **155** | 500 | not a separate bucket here; pinned to the same ceiling so it can never be the looser cap |
| `MAX_CONCURRENT_QUOTED_MARKETS` | **50** | *(does not exist)* | §3.1, rate limit |

> **CAPITAL CAP AS A RESEARCH COST: $200.**
> = $155 open-notional ceiling + $25 daily-loss stop + $20 rounding and fee
> headroom. At most $25 of it is expected to be *spent*; the rest is tied up and
> returned when orders cancel or positions settle.

**A KNOWN BUG THE CAP SITS ON, which the future kit must fix before the cap
means anything.** `OrderRouter._risk_context` computes open notional as
`sum(OrderRow.remaining_size * OrderRow.price)` (`app/execution/router.py:939-943`)
— **with no side awareness**. A resting SELL of 1 contract at price 0.10 counts
as $0.10 against the cap while its actual collateral lock is `(1 - 0.10) x 1 =
$0.90`. For a two-sided market maker, which is short on one side of every quote
by construction, `MAX_OPEN_NOTIONAL_USD` **systematically undercounts the capital
it is supposed to be capping**, and it undercounts worst exactly where the
policy quotes most: cheap markets with wide books. Fixing it is item 3 in §6.

### 4.4 The loss buffer, and the honest admission attached to it

The kit's power table starts at a 500-market portfolio
(`kalshi-honest-holdout.json → …power`), and this test is ~39 trading markets, so
**the existing table cannot supply the band.** A naive normal approximation from
the measured moments gives its shape. `fill_model=pessimistic`, `terminal=settled`,
at `quote_size=1.0` (both moments ÷10 from the cited size-10 values
`mean_pnl_per_trading_market = 0.43598476605005465` and
`sd_pnl_per_trading_market = 6.094081460683052`):

    mean = $0.043598 / trading market      sd = $0.609408 / trading market   [derived]

    k trading markets:  5th pct total = k x 0.043598 - 1.645 x 0.609408 x sqrt(k)

| k | expected total | **naive 5th pct** |
|---:|---:|---:|
| 10 | +$0.44 | **−$2.73** |
| 25 | +$1.09 | **−$3.92** |
| 50 | +$2.18 | **−$4.91** |
| 67 | +$2.92 | **−$5.28** |
| 100 | +$4.36 | **−$5.66** |

**These are labelled `naive` per GUARDRAILS §2.4 and are a placeholder for the
shape, not the band the test runs on.** The band must be recomputed by
event-clustered bootstrap over the same 919-market test pool, at
`quote_size=1.0`, by the prediction re-run in §2.3 — the same
`app.scripts.calibration.cluster_bootstrap` path every interval in this kit used.
A naive i.i.d. band on clustered data is narrower than the truth and would fire
the kill rule late.

`MAX_DAILY_LOSS_USD = 25` is roughly 4x the flattened naive band. **It is a chosen
stop, not a derived bound.** There is no measured bound on aggregate loss in this
kit, and inventing one would be the defect this kit exists to prevent. What
bounds the loss in practice is the 1-contract size, the 5-contract inventory cap,
and the kill rule in §5.2 — not this number.

**Note what the expected-value column says about the whole exercise: +$2.92 of
expected cash P&L across the entire test.** It is a measurement, not a business.

---

## 5. The pass rule and the kill rule

### 5.1 Pass rule

Let `P_markout` be the pessimistic per-contract markout prediction re-derived at
`quote_size=1.0` (§2.3), and `P_fillrate = 0.014655` fills per resting
quote-minute (§1.1, size-invariant per §2.2).

> **PASS — both conditions, on the pessimistic branch, on an event-clustered
> interval:**
>
> 1. `ci95_lower( realized per-contract markout half-spread ) >= P_markout`
> 2. `ci95_lower( realized fill rate per resting quote-minute ) >= P_fillrate`
>
> CI = 95%, **clustered by event**, 500 bootstrap replicates, whole events
> resampled with replacement, via `app.scripts.calibration.cluster_bootstrap` —
> the same function, not a reimplementation. Every observation carries its
> `event` key (GUARDRAILS §2.4). A naive binomial interval on the fill rate may
> be printed beside it, labelled `naive`.

The rule is on the **lower bound**, not the point estimate. A point estimate
above the prediction with an interval straddling it has not established that
`realized >= pessimistic`; it has established that the test was too small to
tell, which is outcome 4 below.

**FAIL** is the interesting outcome. The four cases:

| outcome | reading |
|---|---|
| `realized >= optimistic` | Queue position better than either model. Would need explaining before it was believed; the likeliest explanation is a bug in the markout pipeline, not a discovery. |
| `pessimistic <= realized < optimistic` | **PASS.** The replay's pessimistic branch is honest about queue position, and every verdict in this kit computed on it stands as computed — including the Kalshi NO-GO. |
| `realized < pessimistic` | **FAIL, and it invalidates the fill model, not just this test.** `pessimistic` is not pessimistic enough, and every pessimistic figure this kit has produced is optimistic by the measured gap. This is the outcome the test exists to be able to find. |
| interval too wide to separate | **UNDERPOWERED.** Report `n_fills`, `n_trading`, the per-fill sd and the interval; do not re-run on a widened sample to make the answer come out (GUARDRAILS §2.6). |

**No acceptance criterion anywhere in this design asserts a direction**
(GUARDRAILS §2.5). The test is complete when it has >= 100 fills and a correctly
labelled report, whichever way the number came out.

### 5.2 Kill rule

> **KILL RULE. After every fill, recompute `k` = the number of distinct markets
> that have taken at least one fill. If cumulative P&L (realized cash on fills,
> plus settled terminal inventory, plus marked open inventory at the current
> venue mid) falls below `pct5(k)` — the event-clustered pessimistic 5th
> percentile of total P&L at `k` trading markets, computed at `quote_size=1.0`
> before the test starts (§4.4) — then: CANCEL EVERY RESTING ORDER ON THE VENUE,
> PLACE NOTHING FURTHER, AND STOP THE TEST.**
>
> Stopping is terminal. The test does not resume on a recovery, and the report
> says it was killed, at what `k`, and at what P&L.

Four further stops, each unconditional and each independent of the P&L band:

1. **The kill-switch file.** `app/execution/fences.py::assert_placement_allowed`
   already raises `KillSwitchEngaged` if `Settings.kill_switch_path` exists, and
   `OrderRouter.submit()` already calls it before any leg is planned, reserved or
   placed, **in both paper and live mode**. This exists, is tested
   (`tests/test_fences.py`), and must not be reimplemented. An operator arms it
   with `touch TRADING_KILL_SWITCH`.
2. **`MAX_DAILY_LOSS_USD = 25` breached** — `check_order_limits` already raises
   `RiskLimitExceeded` on this, from both order paths.
3. **A reconciliation pass that cannot read the venue.** `reconcile()` correctly
   refuses to act on an unreadable venue and reports every row unresolved — *"A
   VENUE THAT CANNOT BE READ IS NOT A VENUE WITH NOTHING ON IT"*
   (`app/execution/reconcile.py:24-32`). **But nothing today stops the quoting
   loop when that happens**, and a market maker that keeps quoting into a venue
   it cannot read is accumulating exposure it cannot see. Wiring that stop is
   item 4 in §6.
4. **Any fill at a size other than 1 contract, or on a market not in the
   subscribed set.** Either means the order path is not doing what this design
   says it does.

### 5.3 Labels the report must carry — and a GUARDRAILS amendment this forces

Every figure the live test emits must carry `fill_model=` and `terminal=`
(GUARDRAILS §2.1). **But §2.1's allowed values are
`fill_model ∈ {optimistic, pessimistic}` and `terminal ∈ {settled}`, and neither
can describe a realized live number.** A realized fill is not a fill model, and a
market still open is not `settled`. The amendment is listed in §6.2; until it is
made, a live report cannot be correctly labelled at all. In this document,
figures a live test would produce are written `fill_model=realized`,
`terminal=live` — **label values that do not yet exist.**

### 5.4 Duration

**The binding acceptance is >= 100 fills, not a calendar.** The duration follows
from the arrival rate of qualifying Kalshi markets, and **this kit cannot supply
that rate.** The honest-holdout sample closed 4,569 test markets over 17.18 days,
but 98.1% of them closed in the final 8 days and 1,276 on the single day
2026-09-07 (`kalshi-honest-holdout.md` §2.3, §7) — that is Kalshi's close
calendar and its page-capped settled listing, **not a steady-state arrival rate**,
and using it as one would be the density-versus-span error `go-no-go.md` §3.1
spends a section on.

So:

| | |
|---|---|
| floor | **14 days** — the kit's own pre-committed test-window criterion (`kalshi-honest-holdout.md` §6) |
| target | until >= 100 fills, or 200 markets have been offered to the policy and run to close |
| **hard stop** | **28 days**, whichever comes first; at the stop the report says how many fills were obtained and whether the test is UNDERPOWERED |
| set from | the prerequisite arrival-rate probe in §8.2, not from this sample |

---

## 6. The exact code a future kit must build

### 6.1 What already exists — read it, do not rewrite it

| module | what it already does |
|---|---|
| `app/execution/fences.py` | `assert_live_allowed` (construction-time: `TRADING_MODE=live` AND `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY`); `assert_placement_allowed` (kill switch, **every** order, **both** modes); `check_order_limits` (order / open / daily-loss / bucket caps, strict "exceeds" not "reaches"); `LiveTradingDisabled`, `KillSwitchEngaged`, `RiskLimitExceeded` |
| `app/execution/router.py` | `OrderRouter.submit(intent, strategy) -> RoutedIntent`; `OrderRouter.cancel(order_id) -> CancelResult`; TIF mapping (`GTC` for `best_effort`, `IOC` for `all_or_none`); `snap_to_tick`; PENDING-row-before-venue-call crash safety; the `asyncio.Lock` gate **and its documented one-process limit** |
| `app/execution/reconcile.py` | `reconcile()` — local `PENDING`/`OPEN`/`PARTIALLY_FILLED` rows against `get_open_orders()` + `get_fills(since)`; refuses to act on an unreadable venue; `_fold_buys_into_position`; explicitly does **not** rebuild positions |
| `app/venues/kalshi/live.py` | `KalshiLiveAdapter`, `build_order_body`, `OrderRoute` — **one of the three modules `test_fences.py` permits to contain order placement** |
| `app/venues/polymarket/live.py`, `app/services/polymarket/client.py` | the other two permitted modules |
| `app/strategies/market_making.py` | `MarketMaker.quote(book, tick_size=, inventory=, hours_to_close=) -> QuotePair`. A pure function of book, inventory and parameters. **It places nothing.** |
| `app/scripts/preflight.py` | the arming checklist, including the `TRADING_KILL_SWITCH_PATH`-vs-`KILL_SWITCH_PATH` near-miss trap |
| `backend/tests/test_fences.py` | the AST walk over `app/` enforcing `LIVE_MODULES` + `WRAPPER_MODULE`; `ALLOWED_UNTIL_T16` is empty and a test asserts it stays empty |

**Design goal for the Gate 2 kit: place every order through the existing
`KalshiLiveAdapter`, so `LIVE_MODULES` does not grow by a single entry and
`test_fences.py`'s allowlist is never widened.** A new module that can place an
order is a new hole in the fence; a new module that calls the one module that
already may is not.

### 6.2 What does not exist and must be built

**1. A quoting loop.** Nothing in the repo drives `MarketMaker.quote()` against a
live book on a timer. `OrderRouter.submit()` takes an `Intent` — a matched /
arbitrage object with legs — not a two-sided quote pair, and there is no
scheduler that would call it once a minute per market. Needed: a session object
that, per `(market_id, outcome)`, reads the book, calls `quote()`, diffs the
result against what it believes is resting, and emits the minimum set of
place/cancel operations. It must respect `MAX_CONCURRENT_QUOTED_MARKETS`, and it
must record, per interval, whether it quoted and why not — `QuotePair.reason`
already carries `one_sided_book` / `crossed_or_locked_book` /
`spread_below_minimum` / `inventory_long_limit`, and those are what make the live
`quote_hours` comparable to the replay's.

**2. Cancel/replace with a resting-order identity.** `OrderRouter.cancel()`
cancels by local row id. There is no "replace", and no notion of *"the bid I have
resting in market X"*. Needed: a per-`(market_id, outcome, side)` slot keyed by
`client_order_id`, and an idempotent replace that is **cancel-then-place, never
place-then-cancel** — the latter leaves the maker momentarily double-sized, which
at `max_inventory=5` is a 40% inventory excursion from a bookkeeping choice. The
replace must be a no-op when the newly computed quote rounds to the same tick as
the resting one; §3.1's read-only churn measurement sizes how often that is.

**3. Resting-order inventory and collateral tracking.** Two distinct gaps:
   - *Collateral.* `_risk_context` sums `remaining_size * price` with no side
     awareness (§4.3). Needed: a collateral accountant that computes
     `bid.price * size + (1 - ask.price) * size` — **the same arithmetic as
     `mm_backtest._collateral`, so the live capital number is the same quantity
     the replay's ROC denominator was** — and a cap checked against that, not
     against notional-at-price.
   - *Inventory.* The policy's `inventory` argument must be the venue's truth, not
     a local guess. `reconcile()` writes `Trade` rows and folds BUY fills into a
     position but explicitly does **not** rebuild positions, so a market maker's
     signed inventory per market has no authoritative source today. Needed: a
     per-market signed position, reconciled against `adapter.get_positions()`,
     with an explicit decision recorded for what happens when it disagrees.

**4. Reconciliation on the market maker's clock, and the stop it must drive.**
`reconcile()` is correct for what it does and must be reused, not rewritten. What
must be added around it: (a) run it on the quoting loop's beat, not only on
startup; (b) **stop the quoting loop** when a pass comes back with unresolved
rows because the venue could not be read (§5.2 stop 3); (c) a fill -> markout
pipeline that, for each reconciled fill, records the venue mid **two intervals
later** and computes `mark_to_market(fill, mid(i+2))` — the pass-rule statistic
does not exist unless something goes and marks it, and "two intervals later, never
inside the fill interval" is GUARDRAILS §4.3 and the one property the replay's
own test asserts on.

**5. A one-process guarantee.** `OrderRouter`'s docstring is explicit: the
`asyncio.Lock` is scoped to one event loop in one process, closing the gap needs
a schema change that has deliberately not been made, and *"Until it exists, run
ONE order-routing process."* A Gate 2 kit must enforce that operationally (a
pidfile or an advisory lock the loop takes at startup and refuses to run without),
not document it and hope.

**6. The prediction re-derivation** (§2.3) and **the quote-churn measurement**
(§3.1). Neither is order code; both are read-only re-runs over data already on
disk, and both must complete **before** any order code is written, because they
set the numbers the order code is sized and scored against.

**7. A Polymarket rebate ledger reader** (§7). Nothing in the repo reads a
Polymarket rebate payout. `get_balance()` exists on the adapter protocol; whether
Polymarket exposes a rebate credit as a distinguishable line, and in what
denomination, is unmeasured.

---

## 6.2 The GUARDRAILS changes that kit would need

These are **narrowings with an expiry**, not deletions. Each one names the venue,
the size, the strategy and the arming condition, so that lifting the fence for
this test does not lift it for anything else.

**`market-edge/GUARDRAILS.md` §1.1** — today: *"Never place, modify, or cancel a
real venue order — not from a test, a verify command, a red-team probe, a 'quick
check', or CI."* Amend to:

> A real venue order may be placed, modified or cancelled **only** when **all**
> of the following hold, and the fence tests assert each one:
> 1. venue is **Kalshi** (`app/venues/kalshi/live.py`; Polymarket live placement
>    stays forbidden for the queue arm, and is separately narrowed in §7 for the
>    rebate arm);
> 2. size is exactly **1 contract**;
> 3. the order originates from **`MarketMaker`** with
>    `edge_fraction=0.90, min_spread=0.25, skew_strength=1.0, taper_hours=0.0,
>    quote_size=1.0, max_inventory=5.0` — one strategy, one parameter set;
> 4. `TRADING_MODE=live` **and**
>    `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY` **and** the
>    `TRADING_KILL_SWITCH` file is absent;
> 5. the four caps of §4.3 are in force and `check_order_limits` is called on
>    every order from both paths;
> 6. exactly one order-routing process is running.
>
> **Not from a test, a verify command, a red-team probe, a "quick check", or CI —
> that half of the rule is unchanged and is not narrowed by anything above.**

**`market-edge/GUARDRAILS.md` §1.2** — *"`TRADING_MODE` defaults to `paper` and
stays `paper` in every test process."* **UNCHANGED, and explicitly so.** Exactly
one operator-run session is live. Every test, verifier, red-team and
security-auditor process keeps constructing explicit `Settings(...)` objects, as
`tests/test_fences.py` already does.

**`market-edge/GUARDRAILS.md` §1.4** — no network to venues from tests,
verifiers, red-team or security-auditor. **UNCHANGED.**

**`market-edge/GUARDRAILS.md` §1.5** — fees are never literals in strategy code.
**UNCHANGED, and §2.3 above is why it matters more at size 1, not less**: the
whole-cent fee ceiling is a property of `KalshiFeeModel`, and a kit that hard-coded
"the maker fee is 1.75%" would miss it entirely.

**`mm-proveout/GUARDRAILS.md` §1.1** — today: *"No task writes code that can
place, modify or cancel an order … `backend/tests/test_fences.py` enforces the
allowed-files list by AST; do not add a file to that list."* Amend to:

> Exactly one module in the Gate 2 kit may drive order placement, and it does so
> **through `KalshiLiveAdapter`** rather than by calling a venue client itself.
> **`LIVE_MODULES` does not grow.** `test_fences.py` is **extended** — never
> weakened — with assertions that the quoting loop cannot place an order of size
> != 1, cannot place on a venue other than Kalshi, and cannot run with
> `TRADING_MODE=paper` silently mapping to a live adapter. `ALLOWED_UNTIL_T16`
> stays empty and `test_allowed_until_t16_is_empty` stays passing.

**`mm-proveout/GUARDRAILS.md` §1.3** — *"Network is read-only public GETs … No
POST/PUT/DELETE to any venue."* Narrow to: POST/DELETE permitted **only** to
Kalshi's order endpoints, **only** from `KalshiLiveAdapter`, **only** under §1.1's
six conditions. Every other venue call stays a read-only GET.

**`mm-proveout/GUARDRAILS.md` §1.4** — *"`.env` is never opened by a task."*
**UNCHANGED.** The adapter loads it, as it always has; no task opens it, and no
key appears in any report, log or NOTES entry.

**`mm-proveout/GUARDRAILS.md` §2.1** — **must be amended or a live report cannot
be labelled at all** (§5.3). Add `fill_model ∈ {optimistic, pessimistic,
realized}` and `terminal ∈ {settled, excluded, live}`. `realized` may appear only
on a figure computed from actual venue fills, and `live` only on a figure whose
inventory has not settled. (`excluded` is already in use by
`mm_replay_snapshots.py`'s markout-only mode and should be written into §2.1 at
the same time — see §9, defect 1.)

**`mm-proveout/GUARDRAILS.md` §2.3** — the rebate is its own line, never added
into P&L, `FeeModel.fee()` never credits it. **UNCHANGED, and it stays unchanged
even if §7 observes a rebate arriving.** Observing one payment establishes that a
payment happened once; it does not license crediting a projected payment into
every cost figure in the repo, which is the leak the rule exists to stop.

**`mm-proveout/GUARDRAILS.md` §2.5 and §2.6** — no acceptance criterion asserts a
direction; never widen a threshold to make `n`. **UNCHANGED, and they bind this
test hardest.** A live test that comes back short of 100 fills reports
UNDERPOWERED. It does not get extended to find them.

**New `mm-proveout/GUARDRAILS.md` §1.5** — *A live session runs exactly one
order-routing process, enforced at startup, because `OrderRouter`'s concurrency
gate is process-scoped and says so in its own docstring.*

---

## 7. The Polymarket arm — does the rebate actually get paid?

This is a **separate, smaller instrument** from §1–§6 and answers a different
question. It needs ~10 fills, not 100, and no prediction from a replay. **It does
not change the UNDERPOWERED verdict and is not a step toward changing it.**

### 7.1 Why it is worth specifying despite UNDERPOWERED

"Published" and "paid" are different claims, and this kit has only ever measured
the first. Measured live 2026-09-12T15:55Z over the raw `feeSchedule` object on
the listing payload (`polymarket-feasibility.md` §3, `go-no-go.md` §4.5):

| | `min_spread >= 0.10` (n=130) | `min_spread >= 0.25` (n=75) |
|---|---|---|
| carries a raw `feeSchedule` | 128 (98.5%) | 73 (97.3%) |
| `feesEnabled: false` (explicit zero fee, zero rebate) | 2 (1.5%) | 2 (2.7%) |
| `rebateRate = 0.25` | 79 (60.8%) | 47 (62.7%) |
| `rebateRate = 0.20` | 49 (37.7%) | 26 (34.7%) |
| `takerOnly` | `true` on all 128 | `true` on all 73 |
| parsed `FeeSchedule.source` | `venue_schedule` 130/130 | `venue_schedule` 75/75 |

**And the rebate is larger than the entire realized half-spread this kit has
measured.** `app/venues/types.py:191-196`: *"Kalshi charges makers 1.75% while
Polymarket pays 15-25% of its taker fee back, a swing of ~0.0069 per contract at
p=0.50 — larger than the entire realised half-spread measured for passive quoting
on Kalshi (+0.0051)."* That figure reproduces exactly under this repo's
`rate x p x (1-p) x size` convention at the modal live `rate=0.04` (40.0% of
markets at the 0.10 threshold) and `rebateRate=0.25`:

    0.25 x 0.04 x 0.25  +  0.0175 x 0.25  =  0.0025 + 0.004375 = $0.006875/contract   [derived]

At the other live rate pair (`rate=0.07`, `rebateRate=0.25`) it is
`$0.008750/contract`; at `rebateRate=0.20` with `rate=0.07`, `$0.007875`
(derived). **Whichever pair applies, the rebate is larger than +0.0051 and larger
than the +$0.024131 per-contract markout only by a factor of ~3.** So whether it
is paid is not a rounding question — it is potentially the difference between a
venue being worth quoting and not.

**A public GET can see the published rate and can never see a payment.**
Polymarket settles rebates separately, daily, in pUSD, under terms the venue can
change (`app/venues/types.py:181-189`; `polymarket-feasibility.md` §3).
Confirming one needs a live fill and a later balance check.

### 7.2 Design

| | |
|---|---|
| venue | Polymarket |
| `quote_size` | **the venue minimum, which is NOT 1.** Measured against 1,000 live markets 2026-09-06: `minimum_order_size` was **15 on 962 of them and 5 on 34** (`app/venues/polymarket/adapter.py:1534-1539`). The adapter raises `VenuePayloadError` rather than defaulting, so the per-market value is readable at quote time. Size = that market's own `minimum_order_size`. |
| market selection | markets carrying `feesEnabled: true` and `rebateRate > 0`, at `min_spread >= 0.10` (the collection floor, `settings.book_collection_min_spread`) — **not** 0.25, because the rebate question does not depend on the adverse-selection threshold and 0.10 has 130 candidates against 75 |
| concurrency cap | **5 markets** |
| collateral ceiling | `15 x (1 - 0.90 x 0.10) = $13.65` per two-sided quote (derived, hard bound as in §4.2); `5 x $13.65 = $68.25` instantaneous |
| **capital cap** | **$100** = $68.25 collateral ceiling + $25 loss stop + rounding |
| `n` | **10 maker fills across >= 5 distinct markets and >= 3 distinct calendar days** |
| duration | until 10 fills or 21 days, whichever first |

### 7.3 The measurement, step by step

1. **Before the quote.** Record the market's raw `feeSchedule` object verbatim —
   `feesEnabled`, `rate`, `rebateRate`, `takerOnly` — plus the parsed
   `FeeSchedule.source` and the UTC timestamp. This is the "published" claim, and
   it must be captured at quote time because the venue can change per-market
   schedules (`polymarket-feasibility.md` §6 classifies the exact value mix as
   weather, not structure).
2. **The fill.** One maker fill of `N` shares at price `p`, observed through the
   authenticated adapter's `get_fills()` and `reconcile()`. **Note the
   constraint:** `GET /trades` on Polymarket is 401 without keys
   (`mm-proveout/GUARDRAILS.md` §1.3 names this), so reconciliation against
   `get_fills()` is the *only* evidence available that the fill happened and that
   it happened as a maker.
3. **Predicted rebate if paid as published.** Computed the same way
   `mm_backtest._rebate` computes it — a share of the taker fee the other side
   paid — and reported as `"would add $X if paid as published"`, **beneath** any
   P&L and never inside it (GUARDRAILS §2.3). **Prerequisite: Polymarket's actual
   taker-fee formula must be confirmed from the venue, not assumed.** This repo
   applies a `rate x p x (1-p) x size` convention; whether Polymarket's is the
   same is unmeasured, and using the wrong formula would produce a ratio in step 5
   that is wrong by a factor nobody could see.
4. **After.** Read the account balance at a fixed UTC time on each of the **5
   following days**, and record: whether a credit arrived; the day-lag from the
   fill; the amount; the denomination (pUSD vs USDC); and whether it is
   distinguishable from every other balance movement. If it is not
   distinguishable, that is the finding — say so and stop, rather than
   attributing an unattributable delta.
5. **The statistic.**

       realized_rebate_ratio = realized_credit / predicted_rebate_if_paid_as_published

   reported per fill, with the day-lag, plus the aggregate over the 10 fills.

| ratio | reading |
|---|---|
| ≈ 1.0 | The published rate is paid. Every `"would add $X if paid as published"` line in this kit is a real number that was being correctly withheld from P&L. |
| ≠ 1.0, > 0 | The published rate is **not** the paid rate. Every such line is wrong by that factor, and the factor must be recorded with its measurement date. |
| 0, on all 10 | The published rate is not evidence of a payment. GUARDRAILS §2.3's refusal to credit it was correct, and should be tightened rather than relaxed. |

6. **What 10 fills cannot do.** They can distinguish "never paid" from "paid" and
   put a coarse bound on the ratio. They **cannot** characterize the ratio's
   distribution, detect a rate that varies by market or by day, or establish that
   a payment observed in week one continues in month six. The report must say so
   in its own verdict line.

### 7.4 The cost asymmetry, stated plainly

**The rebate question is answerable in days. The capital committed to answering
it is tied up for months.** Today's Polymarket quotable cohort is bimodal:
~9% is already past its nominal close, and essentially all the rest — 69 of 76 at
`min_spread >= 0.25`, ~127 of 141 at `>= 0.10` — cluster on almost exactly one
date, **2026-12-31**, ~110 days out, with **zero markets closing between roughly
day 8 and day 109** (`polymarket-feasibility.md` §5.1). Median days to close:
≈110.5.

So a fill taken to test the rebate leaves inventory that does not settle for
months, unless it is unwound by crossing the spread — which pays the taker fee
(`rate` measured at 0.03–0.07) and is a separate decision with its own cost. The
$100 cap must be understood as **committed for ~110 days**, not for the 21 days
the measurement takes. A future kit that wants the capital back sooner should
prefer the ~9% of the cohort already past its close, and say how it chose.

*(The specific date is calendar weather and will not recur in this form; the
existence of clustering around salient dates is plausibly structural —
`polymarket-feasibility.md` §6.)*

---

## 8. Entry condition — none of the above is authorized today

### 8.1 The condition, stated as a gate

**All four must hold. Today, none of the first three do.**

1. **A venue carries a GO** — or the user decides, explicitly and in writing,
   that the queue-position question in §0.1 is worth answering on its own,
   independent of the P&L verdict. Today Kalshi is **NO-GO** and Polymarket is
   **UNDERPOWERED** (`go-no-go.md` §7). **This document takes no position on
   whether the second branch should be exercised.** It notes only that no amount
   of further replay retires the question, and that the decision is the user's.
2. **The read-only prerequisites in §8.2 are complete**, and their measured
   numbers have replaced the assumptions this document names as assumptions.
3. **The GUARDRAILS amendments in §6.2 are written and merged**, with
   `tests/test_fences.py` **extended** — never weakened — to enforce the narrowed
   §1.1, and with `LIVE_MODULES` unchanged.
4. **`cd backend && python3 -m app.scripts.preflight` passes** with the §4.3 caps
   in `.env`, including the `TRADING_KILL_SWITCH_PATH`-vs-`KILL_SWITCH_PATH`
   near-miss check it already performs.

### 8.2 Prerequisites, all read-only, all runnable under the current fences

| # | prerequisite | why | cost |
|---|---|---|---|
| 1 | Re-derive the pessimistic prediction at `quote_size=1.0, max_inventory=5.0` (§2.3, command given) | the §1.1 markout prediction embeds a size-10 fee that does not apply at the venue minimum; the pass rule's right-hand side does not exist until this runs | a re-run over an existing cache |
| 2 | Recompute the event-clustered pessimistic 5th-percentile band at `k` in {10, 25, 50, 67, 100} trading markets, at `quote_size=1.0` (§4.4) | the kit's power table starts at 500 markets; the kill rule has no band without this, and the naive placeholder is too narrow | same re-run |
| 3 | Count quote-pair churn between consecutive intervals in `kalshi-honest-1m.json` (§3.1) | sizes the real order rate against the ~10 req/s limit; a replace that changes nothing need not be sent | pure arithmetic on a file |
| 4 | Measure Kalshi's daily arrival rate of markets with book spread `>= 0.25` and the quotable-set size at that threshold (§5.4) | the only Kalshi quotable count in this kit is 643 at `min_spread >= 0.10` (`polymarket-feasibility.md` §1, measured 2026-09-07, `top_n=500` binds); nothing measures 0.25, and duration cannot be set without it | `app/scripts/probe_quotable`-shaped, read-only GETs |
| 5 | Confirm Kalshi's per-market `minimum_order_size` live, and whether the account is a direct member (§2.3) | 1 contract is an adapter default, not a measurement; direct membership changes the fee by up to 33% of the edge | read-only GETs |
| 6 | Confirm Polymarket's taker-fee formula from the venue (§7.3 step 3) | the predicted rebate, and therefore the whole §7 ratio, is computed from it | vendor docs / read-only |

**Items 1–3 need no network at all.** Items 4–6 are read-only public GETs, which
`mm-proveout/GUARDRAILS.md` §1.3 already permits.

---

## 9. Discrepancies found against the existing reports and source, recorded rather than smoothed

Found while sourcing the numbers above. **Each is reported, none is fixed here** —
this task's brief names exactly one output file, and `git status --porcelain`
shows exactly one.

**1. `markout_pnl` is NOT settlement-independent in settled mode, and two
documents say it is.** `go-no-go.md` §3.3 calls it *"the settlement-independent
`markout_pnl`"* and `NOTES.md:1466-1474` says *"Markout is settlement-independent
— it marks each fill forward at mid(i+2)"*. The source disagrees:
`mm_backtest.py:1838-1845` and the module docstring's own formula add
`terminal_inventory * (settle - last_mid)`, which requires a real `settle` and is
therefore settlement-**dependent**. The **+$0.6281/market** figure both documents
quote contains that term.

What is accurate: `mm_replay_snapshots.py`'s `--markout-only` mode
(`_strip_terminal_settlement`) strips the term from **every** row alike, leaving a
pure per-fill statistic, and labels it `terminal=excluded` with
`pnl_basis=CASH_PNL_UNAVAILABLE` precisely so nobody reads it as money.
`go-no-go.md` §8.2's phrasing — *"requires no settlement for its **fill
component**"* — is the correct one. **The bare claim in §3.3 and in NOTES is
not.** This matters for the Polymarket arm's framing: it is *markout-only mode*
that needs no settlement, not `markout_pnl`.

`defect: T13 kind=overstated-claim` — a statistic described as
settlement-independent when only one of its two terms is.

**2. `go-no-go.md` §6 says the fabricated Polymarket cohort figures appear
nowhere in the source tree. They do.** §6 states that "136 markets / 57 events"
and "78 / 34" "Could not be located anywhere in `reports/*`, `NOTES.md`,
`TASKS.md`, `PLAN.md` **or the source tree**". They are in the source tree:

    backend/app/scripts/mm_replay_snapshots.py:126-128
      "Polymarket's OWN measured quotable universe is 136 markets across 57
       events at spread >= 0.10 (78 across 34 events at >= 0.25)"

`NOTES.md:1591-1593` correctly establishes those numbers were *"fabricated by
compaction"* and that the measured figures are **130 / 53** and **75 / 32**
(`polymarket-feasibility.md` §1, §2, 2026-09-12T15:55Z). But NOTES checked only
`polymarket-feasibility.md`, and `go-no-go.md` widened that to the whole source
tree without re-running the search. **A number known to be fabricated is live in
a shipped module docstring, asserted there as "measured".**

`defect: T13 kind=unverified-negative` — a "this appears nowhere" claim over a
scope that was not actually searched.

**3. `--markout-only` is attributed to a task that does not exist.**
`NOTES.md:1437` and `go-no-go.md` §8.2 both credit `--markout-only` to "T21".
`mm-proveout/TASKS.md` has no T21 (its tasks are T1–T6, T7–T13, T15–T18, T20,
T22). `market-edge/TASKS.md` T21 is *"Depth recording: `book_snapshots` table"* —
a different thing. `mm_replay_snapshots.py` was created by mm-proveout **T11**,
and its own docstring carries no task id for the mode. The attribution is
unresolvable from the record. **The code exists and I read it in full; only the
provenance label is wrong.** (Related and benign: `T20`/`T25` inside `app/`
docstrings are market-edge task ids, which collide with mm-proveout's own T20.)

**4. "3 of 4 criteria" vs "4 of 5" — both are right under different
conventions, and a reader should know which.** This task's briefing said Kalshi
met 3 of 4 pre-committed criteria; `go-no-go.md` §1/§7 says four of five.
`market_making.py`'s header says *"Four criteria were fixed in advance; THREE were
met."* The difference is whether **"the CI clears zero"** is counted as a
criterion (go-no-go counts it; `market_making.py` treats it as the result rather
than a precondition). Both then agree on the substance: **`n_trading >= 1000` is
the one that failed, at 919.** Not a defect; an ambiguity worth one sentence
somewhere.

**5. Rebate coverage is quoted as "96–99%" in one report and "97–99%" in
another.** `polymarket-feasibility.md` §3 says 96–99%; `go-no-go.md` §4.5 says
97–99%. The underlying counts settle it: **128/130 = 98.5%** at
`min_spread >= 0.10` and **73/75 = 97.3%** at `>= 0.25` carry a nonzero
`rebateRate`. Cosmetic; recorded so the next reader does not have to re-derive it.

**Two figures in this task's briefing that the reports confirm, checked because
the briefing asked to be checked:** `market_making.py`'s front-of-queue +0.0051
vs behind-the-queue −0.0095 sign flip is verbatim correct; and Kalshi's
`[+0.0365, +0.8615]` at `n_trading=919`, the 39.95% five-market P&L share, and
the 33 CI evaluations all reproduce exactly from
`kalshi-honest-holdout.json`/`kalshi-density-gate.json`.

---

## 10. Standing constraints this document operated under

- **No order was placed, modified or cancelled, and no code capable of doing so
  was written, edited, stubbed or fixtured.** `backend/tests/test_fences.py`'s
  allowlist is unchanged; no file was added to `LIVE_MODULES`,
  `WRAPPER_MODULE` or `ALLOWED_UNTIL_T16`.
- **No recommendation to trade appears above.** A specification, an entry
  condition that is not met, and a decision left where it belongs.
- **`.env` was never opened.** No secret, key id, or address appears in this file.
- **No migration was applied to any database.**
- **Network: nothing.** Every number here came from a file already on disk or
  from calling this repo's own fee model in-process.
- Every P&L, ROC and spread figure carries `fill_model=` and `terminal=`;
  pessimistic is first and every threshold is set on it; the rebate is its own
  line and is inside no P&L figure; every interval is clustered by event, and the
  one naive interval is labelled `naive`.
- Every derived number shows its arithmetic. Where a number could not be sourced,
  §8.2 lists the measurement that would supply it instead of a figure standing in
  for one.
