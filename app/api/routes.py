"""
Router de la API REST.

Todas las rutas están en un único APIRouter para facilitar el montaje
en main.py y el testeo independiente.

Convenciones:
  - get_db: dependency que yield-ea una sesión por request.
  - request.app.state.{...}: acceso a objetos compartidos (scheduler,
    anthropic_client, settings) inicializados en el lifespan de main.py.
  - BackgroundTasks: para jobs que pueden tardar (fetch, process).
    El endpoint responde 202 Accepted inmediatamente.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Generator, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.schemas import (
    ArticleResponse,
    BriefingResponse,
    JobStatusResponse,
    SchedulerUpdateRequest,
    SettingsResponse,
    SourceResponse,
    SourceToggleRequest,
    SyncResultResponse,
)
from app.fetcher.sources_loader import load_and_sync
from app.scheduler.jobs import (
    generate_daily_briefing,
    make_briefing_job,
    make_fetch_job,
    make_process_job,
)
from app.storage.crud import (
    delete_source,
    get_all_sources,
    get_articles_for_date,
    get_briefing_by_date,
    get_latest_briefing,
    get_source_by_id,
    toggle_source,
)

router = APIRouter()


# ─── Dependency ───────────────────────────────────────────────────────────────

def get_db(request: Request) -> Generator[Session, None, None]:
    """Dependencia FastAPI: abre una sesión por request y la cierra al terminar."""
    session_factory = request.app.state.session_factory
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


# ─── Health ───────────────────────────────────────────────────────────────────

@router.get("/health", tags=["health"])
def health_check():
    """Endpoint de salud para monitoring y load balancers."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ─── Sources ──────────────────────────────────────────────────────────────────

@router.get("/sources", response_model=list[SourceResponse], tags=["sources"])
def list_sources(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
):
    """Lista todas las fuentes de noticias. Con `enabled_only=true` filtra las habilitadas."""
    return get_all_sources(db, enabled_only=enabled_only)


@router.get("/sources/{source_id}", response_model=SourceResponse, tags=["sources"])
def get_source(source_id: int, db: Session = Depends(get_db)):
    """Obtiene una fuente por su ID."""
    src = get_source_by_id(db, source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return src


@router.patch(
    "/sources/{source_id}/toggle",
    response_model=SourceResponse,
    tags=["sources"],
)
def toggle_source_endpoint(
    source_id: int,
    body: SourceToggleRequest,
    db: Session = Depends(get_db),
):
    """Habilita o deshabilita una fuente."""
    ok = toggle_source(db, source_id, body.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Source not found")
    db.commit()
    return get_source_by_id(db, source_id)


@router.delete("/sources/{source_id}", status_code=204, tags=["sources"])
def delete_source_endpoint(source_id: int, db: Session = Depends(get_db)):
    """Elimina una fuente y todos sus artículos asociados (cascada)."""
    ok = delete_source(db, source_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Source not found")
    db.commit()


@router.post("/sources/sync", response_model=SyncResultResponse, tags=["sources"])
def sync_sources(request: Request, db: Session = Depends(get_db)):
    """
    Sincroniza el archivo sources.yaml con la base de datos.
    Inserta fuentes nuevas y actualiza las existentes.
    """
    sources_config_path = request.app.state.settings.sources_config_path
    try:
        result = load_and_sync(sources_config_path, db)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Archivo de configuración no encontrado: {sources_config_path}",
        )
    db.commit()
    return SyncResultResponse(
        inserted=result.inserted,
        updated=result.updated,
        unchanged=result.unchanged,
        message=(
            f"Sync completado: {result.inserted} nuevas, "
            f"{result.updated} actualizadas, "
            f"{result.unchanged} sin cambios."
        ),
    )


# ─── Articles ─────────────────────────────────────────────────────────────────

@router.get("/articles", response_model=list[ArticleResponse], tags=["articles"])
def list_articles(
    date_str: Optional[str] = None,
    processed_only: bool = False,
    db: Session = Depends(get_db),
):
    """
    Lista artículos del día.
    - `date_str` (YYYY-MM-DD): fecha a consultar; por defecto, hoy.
    - `processed_only`: si true, solo devuelve artículos procesados por Claude.
    """
    if date_str is not None:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Formato de fecha inválido. Use YYYY-MM-DD.",
            )
    else:
        target_date = date.today()
    return get_articles_for_date(db, target_date, processed_only=processed_only)


# ─── Briefings ────────────────────────────────────────────────────────────────

@router.get("/briefings/latest", response_model=BriefingResponse, tags=["briefings"])
def get_latest_briefing_endpoint(db: Session = Depends(get_db)):
    """Devuelve el briefing más reciente disponible."""
    briefing = get_latest_briefing(db)
    if briefing is None:
        raise HTTPException(status_code=404, detail="No hay briefings disponibles.")
    return briefing


@router.get("/briefings/today", response_model=BriefingResponse, tags=["briefings"])
def get_today_briefing(db: Session = Depends(get_db)):
    """Devuelve el briefing de hoy, si fue generado."""
    briefing = get_briefing_by_date(db, date.today())
    if briefing is None:
        raise HTTPException(
            status_code=404, detail="El briefing de hoy no está disponible aún."
        )
    return briefing


@router.get(
    "/briefings/{date_str}", response_model=BriefingResponse, tags=["briefings"]
)
def get_briefing_for_date(date_str: str, db: Session = Depends(get_db)):
    """Devuelve el briefing de una fecha específica (YYYY-MM-DD)."""
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD."
        )
    briefing = get_briefing_by_date(db, target_date)
    if briefing is None:
        raise HTTPException(
            status_code=404, detail=f"No hay briefing para {date_str}."
        )
    return briefing


# ─── Jobs ─────────────────────────────────────────────────────────────────────

@router.post(
    "/jobs/fetch",
    response_model=JobStatusResponse,
    status_code=202,
    tags=["jobs"],
)
def trigger_fetch(request: Request, background_tasks: BackgroundTasks):
    """
    Dispara manualmente el fetch de todos los feeds RSS.
    Responde 202 inmediatamente; el trabajo corre en background.
    """
    session_factory = request.app.state.session_factory
    sources_config_path = request.app.state.settings.sources_config_path
    job = make_fetch_job(session_factory, sources_config_path)
    background_tasks.add_task(job)
    return JobStatusResponse(
        status="started", message="Fetch iniciado en background."
    )


@router.post(
    "/jobs/process",
    response_model=JobStatusResponse,
    status_code=202,
    tags=["jobs"],
)
def trigger_process(request: Request, background_tasks: BackgroundTasks):
    """
    Dispara manualmente el procesamiento IA de artículos pendientes.
    Responde 202 inmediatamente; el trabajo corre en background.
    """
    session_factory = request.app.state.session_factory
    anthropic_client = request.app.state.anthropic_client
    job = make_process_job(session_factory, anthropic_client)
    background_tasks.add_task(job)
    return JobStatusResponse(
        status="started", message="Procesamiento IA iniciado en background."
    )


@router.post(
    "/jobs/briefing",
    response_model=JobStatusResponse,
    status_code=202,
    tags=["jobs"],
)
def trigger_briefing(request: Request, background_tasks: BackgroundTasks):
    """
    Genera (o regenera) el briefing del día actual.
    Responde 202 inmediatamente; el trabajo corre en background.
    """
    session_factory = request.app.state.session_factory
    job = make_briefing_job(session_factory)
    background_tasks.add_task(job)
    return JobStatusResponse(
        status="started", message="Generación de briefing iniciada en background."
    )


# ─── Settings ─────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=SettingsResponse, tags=["settings"])
def get_settings(request: Request):
    """Devuelve la configuración actual de la aplicación."""
    s = request.app.state.settings
    return SettingsResponse(
        daily_fetch_time=s.daily_fetch_time,
        feed_title=s.feed_title,
        feed_description=s.feed_description,
        feed_base_url=s.feed_base_url,
        sources_config_path=s.sources_config_path,
    )


@router.patch(
    "/settings/scheduler",
    response_model=SettingsResponse,
    tags=["settings"],
)
def update_scheduler_time(request: Request, body: SchedulerUpdateRequest):
    """
    Actualiza la hora del ciclo diario fetch → process → briefing.

    Los tres jobs del scheduler se reprograman automáticamente:
      - fetch_time        → hora base
      - fetch_time + 1h  → procesamiento IA
      - fetch_time + 2h  → generación del briefing

    El cambio es en memoria. Para persistirlo entre reinicios,
    actualiza DAILY_FETCH_TIME en el archivo .env.
    """
    from apscheduler.triggers.cron import CronTrigger

    scheduler = request.app.state.scheduler
    settings = request.app.state.settings

    # Parsear la nueva hora (ya validada por SchedulerUpdateRequest)
    h_str, m_str = body.daily_fetch_time.split(":")
    fetch_h, fetch_m = int(h_str), int(m_str)

    def _offset(h: int, m: int, delta_hours: int) -> tuple[int, int]:
        total = h * 60 + m + delta_hours * 60
        return (total // 60) % 24, total % 60

    process_h, process_m = _offset(fetch_h, fetch_m, 1)
    briefing_h, briefing_m = _offset(fetch_h, fetch_m, 2)

    scheduler.reschedule_job(
        "daily_fetch", trigger=CronTrigger(hour=fetch_h, minute=fetch_m)
    )
    scheduler.reschedule_job(
        "daily_process", trigger=CronTrigger(hour=process_h, minute=process_m)
    )
    scheduler.reschedule_job(
        "daily_briefing", trigger=CronTrigger(hour=briefing_h, minute=briefing_m)
    )

    # Actualizar settings en memoria
    settings.daily_fetch_time = body.daily_fetch_time

    return SettingsResponse(
        daily_fetch_time=settings.daily_fetch_time,
        feed_title=settings.feed_title,
        feed_description=settings.feed_description,
        feed_base_url=settings.feed_base_url,
        sources_config_path=settings.sources_config_path,
    )
