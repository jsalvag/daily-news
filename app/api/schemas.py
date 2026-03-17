"""
Schemas Pydantic para request/response de la API REST.

Separados de los modelos ORM (app/storage/models.py) para no acoplar
la capa HTTP con la capa de persistencia.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


# ─── Sources ──────────────────────────────────────────────────────────────────

class SourceResponse(BaseModel):
    id: int
    name: str
    url: str
    category: str
    source_type: str
    language: str
    enabled: bool
    created_at: Optional[datetime] = None
    last_fetched_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class SourceToggleRequest(BaseModel):
    enabled: bool


class SyncResultResponse(BaseModel):
    inserted: int
    updated: int
    unchanged: int
    message: str


# ─── Articles ─────────────────────────────────────────────────────────────────

class ArticleResponse(BaseModel):
    id: int
    source_id: int
    title: Optional[str] = None
    url: Optional[str] = None
    published_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    ai_headline: Optional[str] = None
    ai_summary: Optional[str] = None
    relevance_score: Optional[float] = None
    processed: bool = False

    model_config = ConfigDict(from_attributes=True)


# ─── Briefings ────────────────────────────────────────────────────────────────

class BriefingResponse(BaseModel):
    id: int
    date: str
    headlines_text: str
    full_text: str
    article_ids: str
    generated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ─── Jobs / Operaciones manuales ──────────────────────────────────────────────

class JobStatusResponse(BaseModel):
    status: str    # "started" | "completed" | "error"
    message: str


# ─── Settings ─────────────────────────────────────────────────────────────────

class SettingsResponse(BaseModel):
    daily_fetch_time: str
    feed_title: str
    feed_description: str
    feed_base_url: str
    sources_config_path: str
    # Nota: las API keys nunca se exponen


class SchedulerUpdateRequest(BaseModel):
    daily_fetch_time: str

    @field_validator("daily_fetch_time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        parts = v.strip().split(":")
        if len(parts) != 2:
            raise ValueError("El formato debe ser HH:MM")
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            raise ValueError("El formato debe ser HH:MM con valores numéricos")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("Hora debe ser 0-23, minuto 0-59")
        return f"{h:02d}:{m:02d}"


# ─── AI Model Config ──────────────────────────────────────────────────────────

class AIModelConfigResponse(BaseModel):
    id: int
    role: str
    provider: str
    model_id: str
    base_url: Optional[str] = None
    is_active: bool
    updated_at: Optional[datetime] = None
    # api_key nunca se expone en respuestas

    model_config = ConfigDict(from_attributes=True)


class AIModelConfigRequest(BaseModel):
    provider: str
    model_id: str
    api_key: Optional[str] = None   # None → sin cambios si ya existe; vacío → limpia
    base_url: Optional[str] = None
