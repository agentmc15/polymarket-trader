Initialize the Polymarket trading application with the following structure:

## Phase 1: Backend Setup

1. Create backend directory with FastAPI project:
   ```bash
   mkdir -p backend/app/{models,schemas,api/routes,services/polymarket,strategies,bots,tasks,utils}
   cd backend
   python -m venv venv
   source venv/bin/activate
   ```

2. Install Python dependencies:
   ```
   fastapi[all]
   uvicorn[standard]
   sqlalchemy[asyncio]
   asyncpg
   alembic
   redis
   celery[redis]
   py-clob-client
   web3
   httpx
   pydantic-settings
   python-dotenv
   pytest
   pytest-asyncio
   factory-boy
   black
   mypy
   ruff
   ```

3. Create pyproject.toml with Black, mypy, and ruff configuration

4. Set up alembic for database migrations:
   ```bash
   alembic init alembic
   ```

5. Create initial app structure:
   - app/main.py - FastAPI application with lifespan
   - app/config.py - Pydantic Settings configuration
   - app/database.py - Async SQLAlchemy setup

## Phase 2: Frontend Setup

1. Create frontend with Vite:
   ```bash
   npm create vite@latest frontend -- --template react-ts
   cd frontend
   ```

2. Install frontend dependencies:
   ```
   @tanstack/react-query
   zustand
   axios
   socket.io-client
   recharts
   date-fns
   clsx
   tailwind-merge
   ```

3. Install and configure TailwindCSS:
   ```bash
   npm install -D tailwindcss postcss autoprefixer
   npx tailwindcss init -p
   ```

4. Install shadcn/ui:
   ```bash
   npx shadcn-ui@latest init
   ```

5. Create frontend structure:
   - src/components/{ui,charts,markets,trading,arbitrage,traders,bots,backtesting}
   - src/hooks/
   - src/services/
   - src/stores/
   - src/types/
   - src/utils/

## Phase 3: Docker Setup

1. Create docker-compose.yml with services:
   - postgres (with timescaledb extension)
   - redis
   - backend (FastAPI)
   - frontend (Nginx serving React build)
   - celery-worker
   - celery-beat

2. Create Dockerfiles:
   - docker/backend/Dockerfile
   - docker/frontend/Dockerfile

## Phase 4: Configuration

1. Create .env.example with all required environment variables
2. Create .gitignore for Python, Node, and secrets
3. Create .env for local development (gitignored)

## Phase 5: Initialize Git

1. Initialize git repository
2. Create initial commit with project structure

Execute each phase sequentially and verify completion before moving to the next phase.
