from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure upload dir exists
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield
    # Shutdown: cleanup if needed


def create_app() -> FastAPI:
    app = FastAPI(
        title="Datasheet Extraction API",
        description="Vision-first extraction pipeline for industrial process datasheets",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from app.api import documents, entities, fields, query, sessions

    app.include_router(sessions.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(fields.router, prefix="/api/v1")
    app.include_router(entities.router, prefix="/api/v1")
    app.include_router(query.router, prefix="/api/v1")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
