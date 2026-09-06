# Polymarket Trading Application

## Project Overview

A multi-venue prediction-market inefficiency engine with Python/FastAPI backend and React/TypeScript frontend. The application scans real-API exchanges (Polymarket and Kalshi), detects pricing inefficiencies, and executes through a unified paper and live trading path. Time-to-resolution is a first-class ranking axis. All trading defaults to paper mode; live mode requires explicit confirmation and structural fences.

Trader mimicry (copy-trading) was removed deliberately: per-wallet samples on prediction markets are too small and too correlated to separate skill from variance.

## Tech Stack

### Backend (Python 3.11+)
- **Framework**: FastAPI with async support
- **ORM**: SQLAlchemy 2.0 with async sessions
- **Database**: PostgreSQL + TimescaleDB extension for time-series
- **Cache/Queue**: Redis + Celery for background tasks
- **Blockchain**: py-clob-client, web3.py for Polygon
- **Testing**: pytest, pytest-asyncio, factory_boy

### Frontend (React 19+)
- **Language**: TypeScript 5.9+
- **Framework**: React 19 with Vite
- **State**: TanStack Query (React Query) + Zustand
- **UI**: Tailwind CSS 4 (no shadcn/ui — there is no `components.json` or `components/ui/` in this repo)
- **Charts**: Recharts for data visualization
- **HTTP**: axios for API calls
- **Build**: Vite for development and production

## Project Structure

```
polymarket-trader/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                 # FastAPI app entry
│   │   ├── config.py               # Settings and env vars
│   │   ├── database.py             # DB connection and sessions
│   │   ├── models/                 # SQLAlchemy models
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── market.py
│   │   │   ├── trade.py
│   │   │   ├── position.py
│   │   │   ├── strategy.py
│   │   │   ├── backtest.py
│   │   │   ├── event_link.py       # Cross-venue event matching
│   │   │   ├── book_snapshot.py    # Recorded order-book snapshots
│   │   │   └── intent.py           # Multi-leg execution intents
│   │   ├── api/                    # API routes
│   │   │   ├── __init__.py
│   │   │   ├── deps.py             # Dependencies
│   │   │   └── routes/
│   │   │       ├── backtesting.py
│   │   │       ├── arbitrage.py
│   │   │       ├── trading.py
│   │   │       └── links.py        # Event link API
│   │   ├── venues/                 # Venue adapters (multi-venue)
│   │   │   ├── __init__.py
│   │   │   ├── base.py             # VenueAdapter protocol
│   │   │   ├── types.py            # Normalized types
│   │   │   ├── fees.py             # Fee models
│   │   │   ├── registry.py         # Venue registry
│   │   │   ├── polymarket/         # Polymarket adapter
│   │   │   └── kalshi/             # Kalshi adapter
│   │   ├── execution/              # Order routing and fills
│   │   │   ├── router.py           # OrderRouter (single entry point)
│   │   │   ├── ledger.py           # Position ledger
│   │   │   ├── fill_engine.py      # SimulatedFillEngine
│   │   │   ├── fences.py           # Live-trading safety checks
│   │   │   └── reconcile.py        # Order reconciliation
│   │   ├── services/               # Business logic
│   │   │   ├── backtesting/        # Backtest engine
│   │   │   │   ├── __init__.py
│   │   │   │   ├── engine.py
│   │   │   │   ├── metrics.py
│   │   │   │   ├── data_replay.py
│   │   │   │   └── sweep.py        # Capital-level decay analysis
│   │   │   ├── matching/           # Event matching service
│   │   │   ├── scoring.py          # Opportunity scoring
│   │   │   ├── scanner.py          # Market scanning
│   │   │   └── data_collector.py   # Public trade tape collection
│   │   ├── strategies/             # Trading strategies
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── cross_venue_arbitrage.py
│   │   │   ├── binary_complement_arbitrage.py
│   │   │   ├── multi_outcome_bundle_arbitrage.py
│   │   │   └── ...other strategies
│   │   ├── scripts/                # CLI tools
│   │   │   └── sweep.py            # Run capital sweep on strategies
│   │   ├── tasks/                  # Background tasks
│   │   │   ├── __init__.py
│   │   │   ├── execution.py
│   │   │   ├── scanner.py
│   │   │   └── backtesting.py
│   │   └── utils/                  # Utilities
│   │       ├── time.py             # TZ-aware datetime helpers
│   │       └── ...
│   ├── tests/                      # Test suite
│   │   ├── conftest.py
│   │   ├── fixtures/               # Recorded venue payloads
│   │   ├── venues/                 # Adapter contract tests
│   │   ├── test_fences.py          # Live-order isolation check
│   │   └── ...
│   ├── alembic/                    # DB migrations
│   │   ├── versions/
│   │   ├── env.py
│   │   └── alembic.ini
│   ├── requirements.txt
│   └── pyproject.toml
├── frontend/
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── components/
│   │   ├── hooks/
│   │   ├── services/
│   │   ├── stores/
│   │   ├── types/
│   │   └── utils/
│   ├── package.json
│   ├── vite.config.ts
│   └── tsconfig.json
├── .env.example                    # Environment variable template
├── CLAUDE.md                       # This file
└── README.md
```

## Commands

### Backend — Development and Testing
```bash
cd backend

# Run all tests (SQLite, no network)
python3 -m pytest -q

# Run tests with coverage
python3 -m pytest --cov=app --cov-report=html --cov-report=term

# Type checking
mypy app/services/backtesting app/strategies/base.py

# Linting
ruff check app/

# Database migrations — offline (no DB needed)
alembic upgrade head --sql

# Capital-level sweep on synthetic data
python3 -m app.scripts.sweep --synthetic --levels 500,5000,50000 --out results.json

# Development server
uvicorn app.main:app --reload --port 8000
```

### Frontend
```bash
cd frontend

# Install dependencies
npm ci --no-audit --no-fund

# Development server
npm run dev

# Build for production
npm run build

# Type check
npx tsc -p tsconfig.app.json --noEmit

# Linting
npm run lint
```

## Code Style

### Python
- Use type hints on ALL functions
- Async functions for I/O operations
- Follow PEP 8 with Black formatting
- Docstrings in Google style format
- Use Pydantic for all data validation
- Prefer composition over inheritance
- Use dependency injection via FastAPI Depends

Example:
```python
from typing import Optional
from pydantic import BaseModel

class MarketData(BaseModel):
    """Market data model with validation."""
    
    condition_id: str
    question: str
    outcome_prices: dict[str, float]
    volume_24h: float
    
async def get_market(
    condition_id: str,
    client: ClobClient = Depends(get_clob_client)
) -> MarketData:
    """Fetch market data from Polymarket."""
    ...
```

### TypeScript/React
- Strict TypeScript with no `any` types
- Functional components with hooks
- Use TanStack Query for all API calls
- Zustand for global state (minimal)
- Component files: PascalCase.tsx
- Utility files: camelCase.ts
- Types in separate `.types.ts` files

Example:
```typescript
interface MarketCardProps {
  market: Market;
  onSelect: (id: string) => void;
}

export function MarketCard({ market, onSelect }: MarketCardProps) {
  const { data, isLoading } = useMarketData(market.id);
  // ...
}
```

## API Design

### REST Endpoints Pattern
```
GET    /api/v1/markets                    # List markets
GET    /api/v1/markets/{id}               # Get market details
GET    /api/v1/markets/{id}/orderbook     # Get orderbook
POST   /api/v1/trading/orders             # Place order
DELETE /api/v1/trading/orders/{id}        # Cancel order
GET    /api/v1/trading/positions          # Get positions
POST   /api/v1/arbitrage/scan             # Scan for opportunities
GET    /api/v1/backtests                  # List backtests
POST   /api/v1/backtests                  # Run backtest
GET    /api/v1/bots                       # List bots
POST   /api/v1/bots/{id}/start            # Start bot
```

### WebSocket Events

Not implemented. `socket.io-client` is listed in `frontend/package.json` but
nothing in this repo emits or subscribes to it, and no backend WebSocket
route exists. The Kalshi adapter's `NotImplementedError` for its own
WebSocket is documented separately in `.claude/skills/kalshi-api/SKILL.md`.
Treat any future WebSocket event contract as new work, not as something
already agreed upon.

## Polymarket Integration

### Key APIs
1. **CLOB API** (`https://clob.polymarket.com`)
   - Trading, orders, positions
   - Requires API key authentication
   
2. **Gamma API** (`https://gamma-api.polymarket.com`)
   - Market metadata, events
   - Public, no auth required

### Authentication
```python
from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    key=PRIVATE_KEY,
    chain_id=137,  # Polygon
    signature_type=1,  # For email/Magic wallet
    funder=FUNDER_ADDRESS
)
client.set_api_creds(client.create_or_derive_api_creds())
```

## Environment Variables

### Application
```bash
TRADING_MODE=paper              # "paper" or "live" (defaults to paper)
LIVE_TRADING_CONFIRMATION=      # Must be "I_UNDERSTAND_REAL_MONEY" to enable live mode
SECRET_KEY=your_secret_key
DEBUG=true
LOG_LEVEL=INFO
```

### Polymarket
```bash
POLYMARKET_PRIVATE_KEY=         # Private key (never commit)
POLYMARKET_FUNDER_ADDRESS=      # Wallet address
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
```

### Kalshi (Trade API v2)
```bash
KALSHI_API_KEY_ID=              # Public identifier
KALSHI_PRIVATE_KEY_PEM=         # RSA private key (never commit)
KALSHI_ENV=demo                 # "demo" or "prod" (defaults to demo)
# KALSHI_BASE_URL=              # Override base URL if needed
KALSHI_TAKER_FEE_RATE=0.07      # Standard taker rate
KALSHI_MAKER_FEE_RATE=0.0
```

### Costs and Risk Management
```bash
POLYMARKET_TAKER_FEE_OVERRIDES={}  # Per-category fee overrides (JSON)
REDEMPTION_GAS_USD=0.05            # Polygon redemption gas cost
TRANSFER_LATENCY_HOURS=72          # ACH/wire settlement between venues
TRANSFER_COST_USD=5.0              # USD cost per venue transfer
LIQUIDITY_FRACTION=0.02            # Synthetic book depth multiplier
NEAR_RESOLUTION_HOURS=72           # Hours-to-close for near-resolution bucket
SETTLEMENT_DELAY_HOURS=24          # Hours post-resolution until payout
MIN_HOURS_FOR_ANNUALIZATION=6
MIN_VIABLE_ANNUALIZED=0.05
MIN_TRADE_USD=10.0                 # Minimum notional to attempt
```

### Live Trading Fences
```bash
KILL_SWITCH_PATH=TRADING_KILL_SWITCH          # File existence halts all orders
MAX_ORDER_NOTIONAL_USD=250
MAX_DAILY_LOSS_USD=100
MAX_OPEN_NOTIONAL_USD=1000
MAX_NEAR_RESOLUTION_NOTIONAL_USD=500
```

### Order Routing
```bash
PAPER_STARTING_BALANCES={"polymarket": 1000.0, "kalshi": 1000.0}
RECONCILE_GRACE_S=120
ALL_OR_NONE_FILL_TOLERANCE=0.995
UNWIND_SLIPPAGE_TICKS=5
```

### Database & Frontend
```bash
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/polymarket
REDIS_URL=redis://localhost:6379
VITE_API_URL=http://localhost:8000/api/v1
```

## Testing Requirements

- All new features must have tests
- Backend: pytest with >80% coverage
- Frontend: no test runner is configured today (`frontend/package.json` has
  no `test` script and does not depend on `vitest` or
  `@testing-library/react`); do not invent a test command that cannot run
- Integration tests for trading flows
- Mock Polymarket API in tests

## Security Considerations

- NEVER commit private keys or API secrets
- Use environment variables for all secrets
- Implement rate limiting on all endpoints
- Validate all user inputs
- Use prepared statements (SQLAlchemy handles this)
- Implement proper CORS policies
- Sign all orders client-side

## Performance Guidelines

- Use async/await for all I/O
- Implement connection pooling for DB
- Cache market data in Redis (TTL: 5s)
- Use WebSocket for real-time updates
- Batch database writes where possible
- Use TimescaleDB hypertables for time-series

## Money Invariants

These are structural safety rules enforced by code and tests. Operators and developers must understand them.

- "`TRADING_MODE` defaults to `paper`; live requires `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY` and no `TRADING_KILL_SWITCH` file"
- "Only `app/venues/*/live.py` (Polymarket, Kalshi) and `app/services/polymarket/client.py` (the sanctioned wrapper declaring the `py_clob_client` calls, callable only from `venues/polymarket/live.py`) may contain venue order placement/cancellation — `tests/test_fences.py` enforces this by AST walk (see `LIVE_MODULES`/`WRAPPER_MODULE` there)"
- "Never place an order from a test, a verifier, or CI"
- "Run exactly ONE order-routing process. The near-resolution bucket cap and the position ledger are serialized by an `asyncio.Lock` scoped to one event loop in one process (`app/execution/router.py`). A second API worker or Celery worker sharing the database can breach the cap and lose filled positions to a concurrent-update overwrite. The portable fix (optimistic concurrency on `positions`) is not implemented."

## Git Workflow

- Branch naming: `feature/`, `fix/`, `refactor/`
- Commit messages: Conventional commits
- PR required for main branch
- Run tests before committing
