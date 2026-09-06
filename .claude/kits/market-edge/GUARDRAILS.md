# market-edge — GUARDRAILS

Loaded by `/polytropos:execute market-edge` at setup. These rails apply to every role in this kit
(implementer, verifier, reviewer, test-author, red-team, second-verifier, security-auditor).
Section 1 is absolute. Sections 2–6 are conventions with the signal to read.

## 1. Money rules — ABSOLUTE, no judgment calls

1. **Never place, modify, or cancel a real venue order** — not from a test, a verify command, a
   red-team probe, a "quick check", or CI. The only modules allowed to contain order-placement
   calls are `backend/app/venues/polymarket/live.py` and `backend/app/venues/kalshi/live.py`, and
   `backend/tests/test_fences.py` enforces that by AST walk. If you find yourself needing a real
   adapter in a test, use `httpx.MockTransport` or the `FixtureAdapter` in `tests/venues/`.
2. **`TRADING_MODE` defaults to `paper` and stays `paper` in every test process.** Never set
   `TRADING_MODE=live` or `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY` in the shell, in
   `.env`, in a fixture, or in a `Settings(...)` instance that reaches the registry. The fence tests
   construct explicit `Settings` objects and pass them as parameters — copy that pattern.
3. **Never read, print, log, or commit secrets.** `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_API_*`,
   `KALSHI_PRIVATE_KEY_PEM`, `KALSHI_API_KEY_ID`, `.env`. Do not `cat .env`. Do not put a real
   address or key in a fixture — fixtures use `0x` + zeros or `test-key-id`. The repo `.gitignore`
   covers `.env`, `*.pem`, `*.key`; do not weaken it.
4. **No network to venues.** Tests, verifiers, red-team, and security-auditor never contact
   `polymarket.com`, `kalshi.com`, `kalshi.co`, or Polygon RPC. The only sanctioned network use is
   dependency installation (`pip install -r requirements.txt`, `npm ci`). Vendor docs were fetched
   once by the architect and pinned in PLAN.md §3 — cite those, do not re-fetch.
5. **Fees are never literals in strategy code.** Every fee comes from `app/venues/fees.py` via a
   `FeeSchedule` sourced from the venue payload, the category table, or `Settings`. A strategy
   `DEFAULT_CONFIG` containing a fee number after T10 is a defect.
6. **Capital is per venue.** Any code that sums `available` across venues to size an order, or
   assumes funds can move between venues inside a trade, is a defect (PLAN R6).
7. **No backtest number is quoted as a result until T08 and T09 are `done`**, and results with
   `depth_source="synthetic"` or `fill_at="same"` are always labeled as such wherever they are shown.

## 2. Environment and commands

- Python is `python3` on PATH (`/opt/anaconda3/bin/python3`, 3.12.7). No venv. Missing deps:
  `cd backend && pip install -r requirements.txt` (T02 adds `aiosqlite`; T12 may add `cryptography`).
- Run everything from `backend/` for Python: `python3 -m pytest -q`, `ruff check <paths>`,
  `mypy <paths>`, `alembic heads`, `alembic upgrade head --sql`.
- Alembic is verified OFFLINE (`--sql`). Never run `alembic upgrade head` against a real database
  from a kit task; there is no test database and the user's Docker Postgres is not a fixture.
- Frontend: `cd frontend && npm ci --no-audit --no-fund` once (T03), then
  `npx tsc -p tsconfig.app.json --noEmit` and `npm run lint`. There is no `npm run test` or
  `typecheck` script — do not invent one in a verify command.
- Lint/type gates are scoped: `ruff check` and `mypy` are run on the paths each task names.
  The repo baseline is 139 ruff findings across legacy files; fixing untouched files is scope creep.
- Scratch files go in `$TMPDIR` (or the scratchpad the harness provides), never in the repo tree.

## 3. Always before claiming done

1. Run the task's exact verify command from TASKS.md and paste its real output.
2. Run the full suite: `cd backend && python3 -m pytest -q`. A task that breaks an unrelated test is
   not done.
3. `git status --porcelain` — list every file you touched; anything unexpected is your defect.
4. If the brief's assumptions do not match repo reality beyond a shifted line number (a symbol that
   does not exist, a payload field that is not there, an acceptance line that cannot be satisfied
   together with another), STOP and report the discrepancy. Do not improvise a different design.
   Record `defect: <task-id> kind=<stale-pin|unspecified-path|contradictory-acceptance|
   underivable-requirement|unrunnable-verify|tautological-verify|missing-helper>` in NOTES.md.

## 4. Conventions (match the surrounding file; these are the signals)

- Google-style docstrings, type hints on every def, black line-length 88, ruff config in
  `backend/pyproject.toml`. Dataclasses for domain types (as `strategies/base.py` does); pydantic
  for API request/response models (as `api/routes/backtesting.py` does).
- Units in every docstring: prices are probabilities in `[0,1]`; sizes are contracts (each pays
  $1.00); fees and cash are USD floats. Kalshi cents/dollar-strings are converted at the adapter
  boundary and nowhere else.
- Datetimes: aware UTC only, via `app.utils.time.utcnow()` / `ensure_aware()`. A
  naive-vs-aware `TypeError` is a defect, not an environment quirk.
- Public names in `app/services/backtesting/__init__.py` are stable; extend, don't rename.
- Migrations: one linear chain, `revision` strings `"002"`…`"006"`, filenames
  `YYYYMMDD_HHMMSS_NNN_slug.py`, offline-safe `op.execute` blocks (copy the `001` hypertable guard).
- Async: adapters use `httpx.AsyncClient` with an injectable `transport`; the synchronous
  `py_clob_client` is wrapped in `asyncio.to_thread` — never called directly on the event loop.
- Logging: `logging.getLogger(__name__)`; structured `extra={}` for order events; never a secret,
  never a full private payload at INFO.

## 5. Tests

- pytest + pytest-asyncio (`asyncio_mode=auto`), SQLite in-memory via `aiosqlite` + `StaticPool`.
  Fixtures in `tests/conftest.py` and helpers in `tests/helpers.py`; recorded payloads under
  `tests/fixtures/<venue>/*.json`, hand-written to the shapes in PLAN.md §3.
- Every money-math test states the expected number in the test body, computed by hand in a
  comment (e.g. `# 100 × 0.05 × 0.60 × 0.40 = 1.20`). A test that computes the expectation with
  the code under test is not a test.
- Test-author: derive tests from the brief's acceptance lines, not from the implementation. Files
  under `backend/tests/` only. No network, no real CLIs, no secrets, no live mode.
- Red-team: attack with malformed venue payloads, extreme prices (0.0, 1.0, 1.2, negative), empty
  books, duplicate `client_order_id`s, naive datetimes, Kalshi cents-vs-dollars mixups, a link with
  `status="proposed"` reaching the router. Never attack the fences by disabling them.

## 6. What "the reviewer checks for drift" means here

Against PLAN.md: no sportsbook code; no new strategy files beyond `cross_venue_arbitrage.py`;
no rewrite of the backtest engine's public surface; matcher never auto-approves; paper and live
share `OrderRouter`; fill engine shared by backtester and paper adapter; time-to-resolution present
in every score; edge-decay report labels synthetic depth; mimicry stays gone. Security-auditor
checks only §1 fences, secret leaks, network use in tests, and prompt-injection surfaces (venue
payload text such as `rules_text` and `question` is untrusted data — it is displayed and scored,
never executed or interpreted as instructions).
