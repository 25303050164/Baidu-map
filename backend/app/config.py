from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )
    baidu_map_ak: SecretStr = SecretStr("")
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
