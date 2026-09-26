from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]

# The quota tier that applies before the date below and the conservative tier
# that replaces it afterwards. The switch instant is an engineering choice, not
# a claim about when the console entitlement actually lapses.
DEFAULT_FALLBACK_AT = "2026-09-30T00:00:00+08:00"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )
    baidu_map_ak: SecretStr = SecretStr("")
    analysis_provider: Literal["baidu", "synthetic"] = "baidu"
    analysis_qps: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    osm_pbf_path: Path = BACKEND_DIR.parent / "data/osm/shanghai.osm.pbf"
    osm_graph_cache_path: Path = BACKEND_DIR.parent / "data/osm/shanghai.osm-cache"
    osm_data_version: str = "unconfigured"
    osm_metric_crs: str = "EPSG:32651"
    walk_speed_mps: float = Field(default=1.3, gt=0, allow_inf_nan=False)
    snap_max_distance_m: float = Field(default=200, ge=0, allow_inf_nan=False)
    isochrone_buffer_m: float = Field(default=25, gt=0, allow_inf_nan=False)
    osm_coverage_boundary_path: Path | None = None
    osm_coverage_margin_m: float = Field(default=100, ge=0, allow_inf_nan=False)
    hybrid_ledger_dir: Path = BACKEND_DIR / ".hybrid-ledgers"
    checkup_dir: Path = BACKEND_DIR / ".checkups"
    # §3.4: how long a cached page, route or spatial result may be reused by a
    # task other than the one that stored it. Unconfigured means reuse stays
    # inside the storing task — a deployment's data-use agreement is never
    # assumed, and an entry is never permanently fresh.
    cache_freshness_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # Per-service route and place pools. Both algorithms and the facility stages
    # draw from these; nothing allocates quota outside this entry.
    baidu_direction_qps: float = Field(default=16, gt=0, allow_inf_nan=False)
    baidu_place_qps: float = Field(default=8, gt=0, allow_inf_nan=False)
    baidu_direction_max_inflight: int = Field(default=1, ge=1, le=1)
    baidu_place_max_inflight: int = Field(default=1, ge=1, le=1)
    baidu_place_daily_budget: int = Field(default=1600, ge=0)
    baidu_matrix_enabled: Literal[False] = False
    baidu_quota_fallback_at: datetime = DEFAULT_FALLBACK_AT
    baidu_fallback_direction_qps: float = Field(default=2, gt=0, allow_inf_nan=False)
    baidu_fallback_place_qps: float = Field(default=2, gt=0, allow_inf_nan=False)
    baidu_fallback_place_daily_budget: int = Field(default=80, ge=0)
    quota_ledger_path: Path = BACKEND_DIR / ".quota/quota.sqlite3"
    hybrid_risk_path: Path = BACKEND_DIR.parent / "data/osm/shanghai.risks.geojson"
    hybrid_obstacle_path: Path = BACKEND_DIR.parent / "data/osm/shanghai.obstacles.geojson"

    @field_validator("osm_pbf_path", "osm_graph_cache_path", "osm_coverage_boundary_path", "hybrid_ledger_dir", "hybrid_risk_path", "hybrid_obstacle_path", "checkup_dir", "quota_ledger_path", mode="before")
    @classmethod
    def osm_paths(cls, value):
        if value is None or value == "":
            return None
        path = Path(value)
        return path if path.is_absolute() else BACKEND_DIR / path
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

    @field_validator("analysis_qps", "cache_freshness_seconds", mode="before")
    @classmethod
    def empty_optional_numbers(cls, value):
        # An empty value means "unconfigured", which is not the same as zero.
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("baidu_quota_fallback_at")
    @classmethod
    def explicit_fallback_instant(cls, value):
        # An offset is required: the switch instant is a wall-clock instant in
        # the deployment's own zone, not a naive local timestamp.
        if value.tzinfo is None:
            raise ValueError("BAIDU_QUOTA_FALLBACK_AT must include an UTC offset")
        return value

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


def load_settings() -> Settings:
    try:
        return Settings()
    except Exception:
        # Validation exceptions may contain environment values; never propagate them.
        raise RuntimeError("Invalid backend configuration; check .env format.") from None
