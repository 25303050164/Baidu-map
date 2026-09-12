from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager

from .config import Settings, load_settings
from .analyses import AnalysisManager, analysis_router


class HealthResponse(BaseModel):
    status: str
    baidu_ak_configured: bool


def create_app(settings: Settings | None = None, *, provider_factory=None) -> FastAPI:
    config = settings if settings is not None else load_settings()
    manager = AnalysisManager(config, provider_factory)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await manager.close()

    app = FastAPI(title="Life Circle Backend", version="0.2.0", debug=False, lifespan=lifespan)
    app.state.analyses = manager
    app.include_router(analysis_router(manager))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", baidu_ak_configured=config.ak_configured)

    return app


app = create_app()
