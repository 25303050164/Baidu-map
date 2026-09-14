from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )
    baidu_map_ak: SecretStr = SecretStr("")
    analysis_provider: Literal["baidu", "synthetic"] = "baidu"
    analysis_qps: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # OSM artifacts are local-only and intentionally optional.  Both names are
    # accepted because older preparation scripts call the location a directory.
    osm_cache_path: Path | None = Field(default=None, validation_alias="OSM_CACHE_PATH")
    osm_cache_dir: Path | None = Field(default=None, validation_alias="OSM_CACHE_DIR")
    osm_cache_version: str | None = Field(default=None, validation_alias="OSM_CACHE_VERSION")
    osm_walk_speed_mps: float = Field(default=1.3, gt=0, allow_inf_nan=False, validation_alias="OSM_WALK_SPEED_MPS")
    cors_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]

    @field_validator("baidu_map_ak", mode="before")
    @classmethod
    def strip_ak(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("analysis_qps", mode="before")
    @classmethod
    def empty_qps(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("osm_cache_path", "osm_cache_dir", "osm_cache_version", mode="before")
    @classmethod
    def empty_osm_values(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("cors_origins")
    @classmethod
    def explicit_origins(cls, values):
        for value in values:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or "*" in value
                or url.username is not None
                or url.password is not None
                or url.path
                or url.query
                or url.fragment
            ):
                raise ValueError("CORS_ORIGINS must contain explicit origins only")
            _ = url.port
        return values

    @property
    def ak_configured(self) -> bool:
        return bool(self.baidu_map_ak.get_secret_value())

    @property
    def osm_cache_location(self) -> Path | None:
        location = self.osm_cache_path or self.osm_cache_dir
        if location is not None and not location.is_absolute():
            return BACKEND_DIR / location
        return location


def load_settings() -> Settings:
    try:
        return Settings()
    except Exception:
        # Validation exceptions may contain environment values; never propagate them.
        raise RuntimeError("Invalid backend configuration; check .env format.") from None
