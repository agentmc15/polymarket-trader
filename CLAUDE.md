# Polymarket Trading Application

## Project Overview

A professional-grade Polymarket trading platform with Python/FastAPI backend and React/TypeScript frontend. The application provides market analysis, automated trading, arbitrage detection, trader shadowing, and comprehensive backtesting capabilities.

## Tech Stack

### Backend (Python 3.11+)
- **Framework**: FastAPI with async support
- **ORM**: SQLAlchemy 2.0 with async sessions
- **Database**: PostgreSQL + TimescaleDB extension for time-series
- **Cache/Queue**: Redis + Celery for background tasks
- **Blockchain**: py-clob-client, web3.py for Polygon
- **Testing**: pytest, pytest-asyncio, factory_boy

### Frontend (React 18+)
- **Language**: TypeScript 5.0+
- **Framework**: React 18 with Vite
- **State**: TanStack Query (React Query) + Zustand
- **UI**: TailwindCSS + shadcn/ui components
- **Charts**: Recharts or Lightweight Charts
- **WebSocket**: Socket.io-client for real-time data
- **Testing**: Vitest, React Testing Library

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
│   │   │   ├── market.py
│   │   │   ├── trade.py
│   │   │   ├── position.py
│   │   │   ├── trader.py
│   │   │   ├── strategy.py
│   │   │   └── backtest.py
│   │   ├── schemas/                # Pydantic schemas
│   │   ├── api/                    # API routes
│   │   │   ├── __init__.py
│   │   │   ├── routes/
│   │   │   │   ├── markets.py
│   │   │   │   ├── trading.py
│   │   │   │   ├── arbitrage.py
│   │   │   │   ├── traders.py
│   │   │   │   ├── backtesting.py
│   │   │   │   └── bots.py
│   │   │   └── deps.py             # Dependencies
│   │   ├── services/               # Business logic
│   │   │   ├── polymarket/         # Polymarket integration
│   │   │   │   ├── client.py       # CLOB client wrapper
│   │   │   │   ├── gamma.py        # Gamma API client
│   │   │   │   └── websocket.py    # Real-time feeds
│   │   │   ├── market_data.py
│   │   │   ├── trading.py
│   │   │   ├── arbitrage.py
│   │   │   ├── trader_analysis.py
│   │   │   ├── copy_trading.py
│   │   │   └── backtesting.py
│   │   ├── strategies/             # Trading strategies
│   │   │   ├── base.py
│   │   │   ├── arbitrage/
│   │   │   ├── momentum/
│   │   │   └── mean_reversion/
│   │   ├── bots/                   # Trading bots
│   │   │   ├── base.py
│   │   │   ├── executor.py
│   │   │   └── manager.py
│   │   ├── tasks/                  # Celery tasks
│   │   │   ├── __init__.py
│   │   │   ├── market_sync.py
│   │   │   ├── trader_tracking.py
│   │   │   └── bot_execution.py
│   │   └── utils/
│   ├── tests/
│   ├── alembic/                    # DB migrations
│   ├── requirements.txt
│   └── pyproject.toml
├── frontend/
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── ui/                 # shadcn components
│   │   │   ├── charts/
│   │   │   ├── markets/
│   │   │   ├── trading/
│   │   │   ├── arbitrage/
│   │   │   ├── traders/
│   │   │   ├── bots/
│   │   │   └── backtesting/
│   │   ├── hooks/
│   │   ├── services/
│   │   ├── stores/
│   │   ├── types/
│   │   └── utils/
│   ├── package.json
│   └── vite.config.ts
├── docker/
├── docs/
├── .env.example
├── docker-compose.yml
└── CLAUDE.md
```

## Commands

### Backend
```bash
# Development
cd backend && uvicorn app.main:app --reload --port 8000

# Run tests
pytest -v

# Run specific test
pytest tests/test_trading.py -v

# Database migrations
alembic upgrade head
alembic revision --autogenerate -m "description"

# Celery worker
celery -A app.tasks worker --loglevel=info

# Celery beat (scheduler)
celery -A app.tasks beat --loglevel=info

# Type checking
mypy app/
```

### Frontend
```bash
# Development
cd frontend && npm run dev

# Build
npm run build

# Run tests
npm run test

# Type checking
npm run typecheck

# Linting
npm run lint
```

### Docker
```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f backend

# Rebuild
docker-compose build --no-cache
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
POST   /api/v1/orders                     # Place order
DELETE /api/v1/orders/{id}                # Cancel order
GET    /api/v1/positions                  # Get positions
GET    /api/v1/traders                    # List tracked traders
GET    /api/v1/traders/{address}/trades   # Get trader's trades
POST   /api/v1/arbitrage/scan             # Scan for opportunities
GET    /api/v1/backtests                  # List backtests
POST   /api/v1/backtests                  # Run backtest
GET    /api/v1/bots                       # List bots
POST   /api/v1/bots/{id}/start            # Start bot
```

### WebSocket Events
```
market:update      # Real-time price updates
orderbook:update   # Orderbook changes
trade:executed     # Trade confirmations
position:update    # Position changes
arbitrage:alert    # Arbitrage opportunities
trader:activity    # Tracked trader actions
```

## Polymarket Integration

### Key APIs
1. **CLOB API** (`https://clob.polymarket.com`)
   - Trading, orders, positions
   - Requires API key authentication
   
2. **Gamma API** (`https://gamma-api.polymarket.com`)
   - Market metadata, events
   - Public, no auth required
   
3. **Data API** (unofficial)
   - Trader data, leaderboards
   - Historical trades

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

```bash
# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/polymarket
REDIS_URL=redis://localhost:6379

# Polymarket
POLYMARKET_PRIVATE_KEY=your_private_key
POLYMARKET_FUNDER_ADDRESS=your_address
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_secret
POLYMARKET_API_PASSPHRASE=your_passphrase

# App
SECRET_KEY=your_secret_key
DEBUG=true
LOG_LEVEL=INFO
```

## Testing Requirements

- All new features must have tests
- Backend: pytest with >80% coverage
- Frontend: Vitest with React Testing Library
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

## Git Workflow

- Branch naming: `feature/`, `fix/`, `refactor/`
- Commit messages: Conventional commits
- PR required for main branch
- Run tests before committing
