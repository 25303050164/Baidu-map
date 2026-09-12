from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import Settings, load_settings


class HealthResponse(BaseModel):
    status: str
    baidu_ak_configured: bool


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else load_settings()
    app = FastAPI(title="Life Circle Backend", version="0.1.0", debug=False)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", baidu_ak_configured=config.ak_configured)

    return app


app = create_app()
