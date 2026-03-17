"""
Rutas de la interfaz web (HTML server-rendered con Jinja2).

Todas las rutas devuelven HTML. Los formularios usan POST + redirect 303
para evitar re-envío al refrescar la página.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api.routes import get_db
from app.storage.crud import (
    delete_model_config,
    get_all_model_configs,
    get_all_sources,
    get_app_setting,
    get_latest_briefing,
    get_model_config,
    get_recent_briefings,
    toggle_source,
    upsert_app_setting,
    upsert_model_config,
)

web_router = APIRouter(prefix="/web", tags=["web"])

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "app" / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ─── Dashboard ────────────────────────────────────────────────────────────────

@web_router.get("/", response_class=HTMLResponse)
def web_index(request: Request, db: Session = Depends(get_db)):
    """Dashboard: briefing más reciente + estado general."""
    briefing = get_latest_briefing(db)
    recent = get_recent_briefings(db, limit=7)
    sources_count = len(get_all_sources(db))
    models = get_all_model_configs(db)
    worker_configured = any(m.role == "worker" for m in models)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "briefing": briefing,
            "recent_briefings": recent,
            "sources_count": sources_count,
            "worker_configured": worker_configured,
        },
    )


# ─── Fuentes ─────────────────────────────────────────────────────────────────

@web_router.get("/sources", response_class=HTMLResponse)
def web_sources(request: Request, db: Session = Depends(get_db)):
    """Lista y gestión de fuentes RSS."""
    sources = get_all_sources(db)
    return templates.TemplateResponse(
        "sources.html",
        {"request": request, "sources": sources},
    )


@web_router.post("/sources/{source_id}/toggle")
def web_toggle_source(
    source_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Habilita/deshabilita una fuente desde la UI."""
    from app.storage.crud import get_source_by_id

    src = get_source_by_id(db, source_id)
    if src is None:
        raise HTTPException(status_code=404)
    toggle_source(db, source_id, not src.enabled)
    db.commit()
    return RedirectResponse("/web/sources", status_code=303)


@web_router.post("/sources/{source_id}/delete")
def web_delete_source(
    source_id: int,
    db: Session = Depends(get_db),
):
    """Elimina una fuente desde la UI."""
    from app.storage.crud import delete_source

    delete_source(db, source_id)
    db.commit()
    return RedirectResponse("/web/sources", status_code=303)


# ─── Modelos ─────────────────────────────────────────────────────────────────

@web_router.get("/models", response_class=HTMLResponse)
def web_models(request: Request, db: Session = Depends(get_db)):
    """Configuración de modelos de IA (worker y editor)."""
    configs = {cfg.role: cfg for cfg in get_all_model_configs(db)}
    return templates.TemplateResponse(
        "models.html",
        {"request": request, "configs": configs},
    )


@web_router.post("/models/{role}")
def web_upsert_model(
    role: str,
    db: Session = Depends(get_db),
    provider: str = Form(...),
    model_id: str = Form(...),
    api_key: str = Form(default=""),
    base_url: str = Form(default=""),
):
    """Guarda la configuración de un modelo desde el formulario."""
    if role not in ("worker", "editor"):
        raise HTTPException(status_code=400, detail="Rol inválido")

    upsert_model_config(
        db,
        role=role,
        provider=provider.strip(),
        model_id=model_id.strip(),
        api_key=api_key.strip() or None,
        base_url=base_url.strip() or None,
    )
    db.commit()
    return RedirectResponse("/web/models", status_code=303)


@web_router.post("/models/{role}/delete")
def web_delete_model(role: str, db: Session = Depends(get_db)):
    """Elimina la configuración de un modelo desde la UI."""
    delete_model_config(db, role)
    db.commit()
    return RedirectResponse("/web/models", status_code=303)


# ─── Ajustes ─────────────────────────────────────────────────────────────────

@web_router.get("/settings", response_class=HTMLResponse)
def web_settings(request: Request, db: Session = Depends(get_db)):
    """Ajustes de la aplicación (hora del scheduler, etc.)."""
    s = request.app.state.settings
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "daily_fetch_time": s.daily_fetch_time,
            "feed_title": s.feed_title,
            "feed_description": s.feed_description,
            "feed_base_url": s.feed_base_url,
        },
    )


@web_router.post("/settings/scheduler")
def web_update_scheduler(
    request: Request,
    db: Session = Depends(get_db),
    daily_fetch_time: str = Form(...),
):
    """Actualiza la hora del scheduler desde la UI."""
    from apscheduler.triggers.cron import CronTrigger

    # Validar formato básico
    parts = daily_fetch_time.strip().split(":")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Formato inválido (HH:MM)")
    try:
        fetch_h, fetch_m = int(parts[0]), int(parts[1])
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato inválido (HH:MM)")
    if not (0 <= fetch_h <= 23 and 0 <= fetch_m <= 59):
        raise HTTPException(status_code=400, detail="Hora o minuto fuera de rango")

    def _offset(h: int, m: int, delta: int) -> tuple[int, int]:
        t = h * 60 + m + delta * 60
        return (t // 60) % 24, t % 60

    process_h, process_m = _offset(fetch_h, fetch_m, 1)
    briefing_h, briefing_m = _offset(fetch_h, fetch_m, 2)

    scheduler = request.app.state.scheduler
    scheduler.reschedule_job("daily_fetch", trigger=CronTrigger(hour=fetch_h, minute=fetch_m))
    scheduler.reschedule_job("daily_process", trigger=CronTrigger(hour=process_h, minute=process_m))
    scheduler.reschedule_job("daily_briefing", trigger=CronTrigger(hour=briefing_h, minute=briefing_m))

    time_str = f"{fetch_h:02d}:{fetch_m:02d}"
    upsert_app_setting(db, "daily_fetch_time", time_str)
    db.commit()
    request.app.state.settings.daily_fetch_time = time_str

    return RedirectResponse("/web/settings", status_code=303)
