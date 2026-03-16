"""
Operaciones CRUD sobre la base de datos.

Usa la API moderna de SQLAlchemy 2.0:
  - select() + session.execute().scalars()  → en vez de session.query() (legacy)
  - update() / delete() con .where()        → operaciones bulk eficientes
  - session.get()                           → lookup por PK

Convención: todas las funciones reciben una `Session` como primer argumento
y NO hacen commit — el caller decide cuándo commitear (facilita tests y
transacciones compuestas).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import select, update, delete, func
from sqlalchemy.orm import Session

from app.storage.models import AIModelConfig, AppSetting, Article, DailyBriefing, Source
from app.fetcher.rss import FetchedArticle


# ─── Sources ──────────────────────────────────────────────────────────────────

def get_all_sources(session: Session, enabled_only: bool = False) -> list[Source]:
    """Retorna todas las fuentes, opcionalmente solo las habilitadas."""
    stmt = select(Source)
    if enabled_only:
        stmt = stmt.where(Source.enabled.is_(True))
    return list(session.execute(stmt).scalars().all())


def get_sources_by_category(session: Session, category: str) -> list[Source]:
    """Retorna fuentes habilitadas de una categoría específica."""
    stmt = (
        select(Source)
        .where(Source.category == category)
        .where(Source.enabled.is_(True))
    )
    return list(session.execute(stmt).scalars().all())


def get_source_by_url(session: Session, url: str) -> Optional[Source]:
    """Busca una fuente por URL exacta."""
    stmt = select(Source).where(Source.url == url)
    return session.execute(stmt).scalars().first()


def get_source_by_id(session: Session, source_id: int) -> Optional[Source]:
    """Busca una fuente por ID (usa session.get para lookup por PK)."""
    return session.get(Source, source_id)


def toggle_source(session: Session, source_id: int, enabled: bool) -> bool:
    """
    Habilita o deshabilita una fuente.

    Returns:
        True si la fuente existía y fue modificada, False si no se encontró.
    """
    src = session.get(Source, source_id)
    if src is None:
        return False
    src.enabled = enabled
    return True


def delete_source(session: Session, source_id: int) -> bool:
    """
    Elimina una fuente y todos sus artículos asociados.

    Returns:
        True si existía y fue eliminada.
    """
    src = session.get(Source, source_id)
    if src is None:
        return False
    session.delete(src)
    return True


def mark_source_fetched(session: Session, source_id: int) -> None:
    """Actualiza last_fetched_at de una fuente al momento actual."""
    session.execute(
        update(Source)
        .where(Source.id == source_id)
        .values(last_fetched_at=datetime.utcnow())
    )


# ─── Articles ─────────────────────────────────────────────────────────────────

def article_exists(session: Session, guid: str, source_id: int) -> bool:
    """Verifica si ya existe un artículo por guid + source_id."""
    stmt = (
        select(func.count())
        .select_from(Article)
        .where(Article.guid == guid)
        .where(Article.source_id == source_id)
    )
    count: int = session.execute(stmt).scalar_one()
    return count > 0


def save_articles(
    session: Session,
    fetched: list[FetchedArticle],
    source_id: int,
) -> int:
    """
    Persiste artículos nuevos de una fuente (deduplicados por guid).

    Args:
        session:   Sesión activa.
        fetched:   Artículos crudos del fetcher.
        source_id: ID de la fuente a la que pertenecen.

    Returns:
        Cantidad de artículos efectivamente insertados.
    """
    # Cargar guids existentes para esta fuente en un set (O(1) lookup)
    existing_guids: set[str] = set(
        session.execute(
            select(Article.guid).where(Article.source_id == source_id)
        ).scalars().all()
    )

    inserted = 0
    for fa in fetched:
        if fa.guid in existing_guids:
            continue

        session.add(Article(
            source_id=source_id,
            title=fa.title,
            url=fa.url,
            guid=fa.guid,
            summary=fa.summary,
            content=fa.content,
            published_at=fa.published_at,
        ))
        existing_guids.add(fa.guid)   # evitar duplicados dentro del mismo batch
        inserted += 1

    return inserted


def get_articles_for_date(
    session: Session,
    target_date: date,
    processed_only: bool = False,
) -> list[Article]:
    """
    Retorna artículos publicados en un día específico.

    Args:
        target_date:    Fecha a consultar.
        processed_only: Si True, solo devuelve artículos procesados por LLM.
    """
    day_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    day_end   = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)

    stmt = (
        select(Article)
        .where(Article.published_at >= day_start)
        .where(Article.published_at <= day_end)
        .order_by(Article.relevance_score.desc().nulls_last(), Article.published_at.desc())
    )
    if processed_only:
        stmt = stmt.where(Article.processed.is_(True))

    return list(session.execute(stmt).scalars().all())


def get_unprocessed_articles(
    session: Session,
    limit: int = 100,
) -> list[Article]:
    """Artículos que aún no fueron procesados por LLM."""
    stmt = (
        select(Article)
        .where(Article.processed.is_(False))
        .order_by(Article.published_at.desc().nulls_last())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def update_article_ai(
    session: Session,
    article_id: int,
    ai_headline: str,
    ai_summary: str,
    relevance_score: float,
) -> None:
    """Guarda los resultados del procesamiento LLM en un artículo."""
    session.execute(
        update(Article)
        .where(Article.id == article_id)
        .values(
            ai_headline=ai_headline,
            ai_summary=ai_summary,
            relevance_score=relevance_score,
            processed=True,
        )
    )


# ─── DailyBriefing ────────────────────────────────────────────────────────────

def get_briefing_by_date(session: Session, target_date: date) -> Optional[DailyBriefing]:
    """Retorna el briefing diario de una fecha, o None si no existe."""
    date_str = target_date.strftime("%Y-%m-%d")
    stmt = select(DailyBriefing).where(DailyBriefing.date == date_str)
    return session.execute(stmt).scalars().first()


def upsert_briefing(
    session: Session,
    target_date: date,
    headlines_text: str,
    full_text: str,
    article_ids: list[int],
) -> DailyBriefing:
    """
    Crea o reemplaza el briefing de una fecha.

    Si ya existe un briefing para esa fecha, lo actualiza.
    Si no existe, lo crea.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    ids_str  = ",".join(str(i) for i in article_ids)

    briefing = get_briefing_by_date(session, target_date)

    if briefing is None:
        briefing = DailyBriefing(
            date=date_str,
            headlines_text=headlines_text,
            full_text=full_text,
            article_ids=ids_str,
        )
        session.add(briefing)
    else:
        briefing.headlines_text = headlines_text
        briefing.full_text      = full_text
        briefing.article_ids    = ids_str
        briefing.generated_at   = datetime.utcnow()

    return briefing


def get_latest_briefing(session: Session) -> Optional[DailyBriefing]:
    """Retorna el briefing más reciente disponible."""
    stmt = (
        select(DailyBriefing)
        .order_by(DailyBriefing.date.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def get_recent_briefings(session: Session, limit: int = 7) -> list[DailyBriefing]:
    """Retorna los N briefings más recientes, del más nuevo al más antiguo."""
    stmt = (
        select(DailyBriefing)
        .order_by(DailyBriefing.date.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


# ─── AIModelConfig ────────────────────────────────────────────────────────────

def get_model_config(session: Session, role: str) -> Optional[AIModelConfig]:
    """Retorna la configuración de modelo para un rol, o None si no existe."""
    stmt = select(AIModelConfig).where(AIModelConfig.role == role)
    return session.execute(stmt).scalars().first()


def get_all_model_configs(session: Session) -> list[AIModelConfig]:
    """Retorna todas las configuraciones de modelos."""
    stmt = select(AIModelConfig).order_by(AIModelConfig.role)
    return list(session.execute(stmt).scalars().all())


def upsert_model_config(
    session: Session,
    role: str,
    provider: str,
    model_id: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> AIModelConfig:
    """Crea o actualiza la configuración de modelo para un rol."""
    cfg = get_model_config(session, role)
    if cfg is None:
        cfg = AIModelConfig(
            role=role,
            provider=provider,
            model_id=model_id,
            api_key=api_key,
            base_url=base_url,
        )
        session.add(cfg)
    else:
        cfg.provider   = provider
        cfg.model_id   = model_id
        cfg.api_key    = api_key
        cfg.base_url   = base_url
        cfg.updated_at = datetime.utcnow()
    return cfg


def delete_model_config(session: Session, role: str) -> bool:
    """Elimina la configuración de modelo para un rol."""
    cfg = get_model_config(session, role)
    if cfg is None:
        return False
    session.delete(cfg)
    return True


# ─── AppSetting ───────────────────────────────────────────────────────────────

def get_app_setting(session: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    """Retorna el valor de un setting por clave, o default si no existe."""
    setting = session.get(AppSetting, key)
    if setting is None:
        return default
    return setting.value


def upsert_app_setting(session: Session, key: str, value: str) -> None:
    """Crea o actualiza un setting de la aplicación."""
    setting = session.get(AppSetting, key)
    if setting is None:
        session.add(AppSetting(key=key, value=value))
    else:
        setting.value      = value
        setting.updated_at = datetime.utcnow()


# ─── TTS config ───────────────────────────────────────────────────────────────

# Valores por defecto para cada clave TTS
_TTS_DEFAULTS: dict[str, str] = {
    "tts_provider":              "disabled",
    "tts_openai_api_key":        "",
    "tts_openai_voice":          "nova",
    "tts_openai_model":          "tts-1-hd",
    "tts_elevenlabs_api_key":    "",
    "tts_elevenlabs_voice_id":   "",
}


def get_tts_config(session: Session) -> dict[str, str]:
    """
    Lee la configuración TTS completa desde app_settings.

    Retorna un dict con todas las claves TTS, usando defaults para las ausentes.
    """
    return {
        key: (get_app_setting(session, key) or default)
        for key, default in _TTS_DEFAULTS.items()
    }


def save_tts_config(session: Session, config: dict[str, str]) -> None:
    """
    Persiste las claves TTS en app_settings.

    Solo guarda las claves definidas en _TTS_DEFAULTS.
    """
    for key in _TTS_DEFAULTS:
        value = config.get(key)
        if value is not None:
            upsert_app_setting(session, key, value)


def update_briefing_audio(session: Session, briefing_id: int, audio_filename: str) -> None:
    """Asocia un archivo de audio a un briefing existente."""
    session.execute(
        update(DailyBriefing)
        .where(DailyBriefing.id == briefing_id)
        .values(audio_filename=audio_filename)
    )


# ─── Article listing ──────────────────────────────────────────────────────────

def get_recent_articles(
    session: Session,
    limit: int = 100,
    offset: int = 0,
    processed: Optional[bool] = None,
    source_id: Optional[int] = None,
) -> list[Article]:
    """
    Retorna artículos recientes ordenados por fecha de fetch descendente.

    Args:
        limit:     Máximo de artículos a devolver.
        offset:    Cuántos saltar (para paginación).
        processed: True → solo procesados, False → solo pendientes, None → todos.
        source_id: Si se provee, filtra por fuente.
    """
    stmt = (
        select(Article)
        .order_by(Article.fetched_at.desc(), Article.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if processed is True:
        stmt = stmt.where(Article.processed.is_(True))
    elif processed is False:
        stmt = stmt.where(Article.processed.is_(False))
    if source_id is not None:
        stmt = stmt.where(Article.source_id == source_id)
    return list(session.execute(stmt).scalars().all())


def count_articles(
    session: Session,
    processed: Optional[bool] = None,
    source_id: Optional[int] = None,
) -> int:
    """Cuenta artículos con los mismos filtros que get_recent_articles."""
    stmt = select(func.count()).select_from(Article)
    if processed is True:
        stmt = stmt.where(Article.processed.is_(True))
    elif processed is False:
        stmt = stmt.where(Article.processed.is_(False))
    if source_id is not None:
        stmt = stmt.where(Article.source_id == source_id)
    return session.execute(stmt).scalar_one()
