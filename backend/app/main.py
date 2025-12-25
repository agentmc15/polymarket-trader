"""FastAPI application entry point."""
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.config import settings
from app.database import close_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """Application lifespan handler.

    Handles startup and shutdown events for the application.

    Args:
        app: FastAPI application instance.

    Yields:
        None: After startup, before shutdown.
    """
    # Startup
    try:
        await init_db()
    except Exception as e:
        # Log but don't fail - allows running without DB for development
        print(f"Warning: Could not connect to database: {e}")

    yield

    # Shutdown
    await close_db()


def create_application() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        FastAPI: Configured application instance.
    """
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # Configure CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include API router
    app.include_router(api_router, prefix="/api/v1")

    return app


app = create_application()


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint.

    Returns:
        dict: Health status.
    """
    return {"status": "healthy"}
