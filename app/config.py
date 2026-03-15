"""Configuración central de la aplicación."""

from typing import Optional

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Claude / LLM API (opcional — los modelos se configuran desde la UI)
    anthropic_api_key: Optional[str] = Field(default=None, env="ANTHROPIC_API_KEY")

    # Servidor
    host: str = Field(default="0.0.0.0", env="HOST")
    port: int = Field(default=8000, env="PORT")

    # Base de datos
    database_url: str = Field(
        default="sqlite:///./data/news.db", env="DATABASE_URL"
    )

    # Scheduler (puede sobreescribirse desde la BD en startup)
    daily_fetch_time: str = Field(default="06:00", env="DAILY_FETCH_TIME")

    # RSS feed
    feed_title: str = Field(default="Daily News Briefing", env="FEED_TITLE")
    feed_description: str = Field(
        default="Tu resumen diario de noticias", env="FEED_DESCRIPTION"
    )
    feed_base_url: str = Field(
        default="http://localhost:8000", env="FEED_BASE_URL"
    )

    # Config de fuentes
    sources_config_path: str = Field(
        default="config/sources.yaml", env="SOURCES_CONFIG_PATH"
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


def get_settings() -> Settings:
    return Settings()
