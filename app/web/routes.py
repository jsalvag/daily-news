"""
Rutas de la interfaz web (HTML server-rendered con Jinja2).

Todas las rutas devuelven HTML. Los formularios usan POST + redirect 303
para evitar re-envío al refrescar la página.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api.routes import get_db
from app.storage.crud import (
    count_articles,
    create_source,
    delete_model_config,
    get_all_model_configs,
    get_all_sources,
    get_app_setting,
    get_all_source_tags,
    get_distinct_categories,
    get_latest_briefing,
    get_model_config,
    get_pipeline_slots,
    get_recent_articles,
    get_recent_briefings,
    get_tts_config,
    get_worker_system_prompt,
    save_pipeline_slots,
    save_tts_config,
    save_worker_system_prompt,
    toggle_source,
    update_source,
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
    sources = get_all_sources(db)
    sources_count = len(sources)
    models = get_all_model_configs(db)
    worker_configured = any(m.role == "worker" for m in models)
    pending_count = count_articles(db, processed=False)
    processed_count = count_articles(db, processed=True)
    total_count = pending_count + processed_count

    # Últimos 6 artículos procesados para la preview del dashboard
    recent_processed = get_recent_articles(db, limit=6, processed=True)
    # Artículos pendientes más recientes
    recent_pending = get_recent_articles(db, limit=4, processed=False)

    source_map = {s.id: s.name for s in sources}

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "briefing": briefing,
            "recent_briefings": recent,
            "sources_count": sources_count,
            "worker_configured": worker_configured,
            "pending_count": pending_count,
            "processed_count": processed_count,
            "total_count": total_count,
            "recent_processed": recent_processed,
            "recent_pending": recent_pending,
            "source_map": source_map,
        },
    )


# ─── Artículos ───────────────────────────────────────────────────────────────

PAGE_SIZE = 50

@web_router.get("/articles", response_class=HTMLResponse)
def web_articles(
    request: Request,
    db: Session = Depends(get_db),
    page: int = 1,
    processed: str = "all",   # "all" | "yes" | "no"
    source_id: int = None,
):
    """Lista paginada de artículos con filtros."""
    processed_flag: bool | None = None
    if processed == "yes":
        processed_flag = True
    elif processed == "no":
        processed_flag = False

    offset = (page - 1) * PAGE_SIZE
    articles = get_recent_articles(
        db, limit=PAGE_SIZE, offset=offset,
        processed=processed_flag, source_id=source_id,
    )
    total = count_articles(db, processed=processed_flag, source_id=source_id)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    sources = get_all_sources(db)
    source_map = {s.id: s.name for s in sources}

    return templates.TemplateResponse(
        "articles.html",
        {
            "request": request,
            "articles": articles,
            "source_map": source_map,
            "sources": sources,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "processed_filter": processed,
            "source_id_filter": source_id,
            "page_size": PAGE_SIZE,
        },
    )


# ─── Fuentes ─────────────────────────────────────────────────────────────────

@web_router.get("/sources", response_class=HTMLResponse)
def web_sources(request: Request, db: Session = Depends(get_db)):
    """Lista y gestión de fuentes RSS."""
    sources = get_all_sources(db)
    categories = get_distinct_categories(db)
    all_tags = get_all_source_tags(db)
    return templates.TemplateResponse(
        "sources.html",
        {
            "request": request,
            "sources": sources,
            "categories": categories,
            "all_tags": all_tags,
        },
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
    new_state = not src.enabled
    toggle_source(db, source_id, new_state)
    db.commit()
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True, "enabled": new_state})
    return RedirectResponse("/web/sources", status_code=303)


@web_router.post("/sources/{source_id}/delete")
def web_delete_source(
    source_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Elimina una fuente desde la UI."""
    from app.storage.crud import delete_source

    delete_source(db, source_id)
    db.commit()
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True})
    return RedirectResponse("/web/sources", status_code=303)


@web_router.post("/sources/create")
def web_create_source(
    db: Session = Depends(get_db),
    name: str = Form(...),
    url: str = Form(...),
    category: str = Form(...),
    source_type: str = Form(default="rss"),
    language: str = Form(default="es"),
    instructions: str = Form(default=""),
):
    """Crea una nueva fuente desde el formulario y extrae tags automáticamente."""
    from app.processor.llm import extract_tags_from_instructions

    tags = _extract_tags_if_possible(db, instructions)
    src = create_source(
        db,
        name=name,
        url=url,
        category=category,
        source_type=source_type,
        language=language,
        instructions=instructions or None,
        tags=tags or None,
    )
    db.commit()
    return RedirectResponse("/web/sources", status_code=303)


@web_router.post("/sources/{source_id}/update")
def web_update_source(
    source_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    url: str = Form(...),
    category: str = Form(...),
    source_type: str = Form(default="rss"),
    language: str = Form(default="es"),
    instructions: str = Form(default=""),
    enabled: str = Form(default=""),
):
    """Actualiza una fuente existente y re-extrae tags si cambiaron las instrucciones."""
    from app.storage.crud import get_source_by_id

    src = get_source_by_id(db, source_id)
    if src is None:
        raise HTTPException(status_code=404)

    # Re-extraer tags solo si las instrucciones cambiaron
    new_instructions = instructions.strip() or None
    tags = src.tags_list  # conservar los existentes por defecto
    if new_instructions != src.instructions:
        tags = _extract_tags_if_possible(db, instructions) or tags

    is_enabled = (enabled == "on")
    update_source(
        db,
        source_id=source_id,
        name=name,
        url=url,
        category=category,
        source_type=source_type,
        language=language,
        instructions=new_instructions,
        tags=tags or None,
        enabled=is_enabled,
    )
    db.commit()
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({
            "ok": True,
            "id": source_id,
            "name": name,
            "url": url,
            "category": category,
            "source_type": source_type,
            "language": language,
            "instructions": new_instructions or "",
            "enabled": is_enabled,
            "tags": tags or [],
        })
    return RedirectResponse("/web/sources", status_code=303)


def _extract_tags_if_possible(db: Session, instructions: str) -> list[str]:
    """Extrae tags usando el modelo Worker si está configurado. Falla silenciosamente."""
    if not instructions or not instructions.strip():
        return []
    try:
        from app.processor.llm import extract_tags_from_instructions
        model_cfg = get_model_config(db, "worker")
        if model_cfg is None:
            return []
        return extract_tags_from_instructions(
            instructions,
            model=model_cfg.litellm_model,
            api_key=model_cfg.api_key,
            base_url=model_cfg.base_url,
        )
    except Exception:
        return []


# ─── Modelos ─────────────────────────────────────────────────────────────────

@web_router.get("/models", response_class=HTMLResponse)
def web_models(request: Request, db: Session = Depends(get_db)):
    """Configuración de modelos de IA (worker y editor) + instrucciones del worker."""
    from app.processor.llm import DEFAULT_SYSTEM_PROMPT

    configs = {cfg.role: cfg for cfg in get_all_model_configs(db)}
    worker_prompt = get_worker_system_prompt(db) or DEFAULT_SYSTEM_PROMPT
    return templates.TemplateResponse(
        "models.html",
        {"request": request, "configs": configs, "worker_prompt": worker_prompt},
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
    """Ajustes de la aplicación (scheduler, TTS, feed, etc.)."""
    s = request.app.state.settings
    tts = get_tts_config(db)
    slots = get_pipeline_slots(db)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "daily_fetch_time": s.daily_fetch_time,
            "slots": slots,
            "feed_title": s.feed_title,
            "feed_description": s.feed_description,
            "feed_base_url": s.feed_base_url,
            "tts": tts,
        },
    )


@web_router.post("/models/worker-prompt")
def web_update_worker_prompt(
    db: Session = Depends(get_db),
    worker_prompt: str = Form(...),
):
    """Guarda el system prompt del modelo worker desde el formulario."""
    save_worker_system_prompt(db, worker_prompt.strip())
    db.commit()
    return RedirectResponse("/web/models#worker-prompt", status_code=303)


@web_router.get("/models/worker-prompt/reset")
def web_reset_worker_prompt(db: Session = Depends(get_db)):
    """Elimina el system prompt personalizado (restaura el predeterminado)."""
    from sqlalchemy import delete as sa_delete
    from app.storage.models import AppSetting

    db.execute(sa_delete(AppSetting).where(AppSetting.key == "worker_system_prompt"))
    db.commit()
    return RedirectResponse("/web/models#worker-prompt", status_code=303)


@web_router.post("/settings/tts")
def web_update_tts(
    db: Session = Depends(get_db),
    provider: str = Form(default="disabled"),
    gtts_lang: str = Form(default="es"),
    gtts_tld: str = Form(default="com.mx"),
    edge_voice: str = Form(default="es-MX-DaliaNeural"),
    openai_api_key: str = Form(default=""),
    openai_voice: str = Form(default="nova"),
    openai_model: str = Form(default="tts-1-hd"),
    elevenlabs_api_key: str = Form(default=""),
    elevenlabs_voice_id: str = Form(default=""),
    google_api_key: str = Form(default=""),
    google_voice: str = Form(default="es-MX-Standard-A"),
    google_language_code: str = Form(default="es-MX"),
):
    """Guarda la configuración TTS desde el formulario."""
    save_tts_config(db, {
        "tts_provider":             provider.strip(),
        "tts_gtts_lang":            gtts_lang.strip(),
        "tts_gtts_tld":             gtts_tld.strip(),
        "tts_edge_voice":           edge_voice.strip(),
        "tts_openai_api_key":       openai_api_key.strip(),
        "tts_openai_voice":         openai_voice.strip(),
        "tts_openai_model":         openai_model.strip(),
        "tts_elevenlabs_api_key":   elevenlabs_api_key.strip(),
        "tts_elevenlabs_voice_id":  elevenlabs_voice_id.strip(),
        "tts_google_api_key":       google_api_key.strip(),
        "tts_google_voice":         google_voice.strip(),
        "tts_google_language_code": google_language_code.strip(),
    })
    db.commit()
    return RedirectResponse("/web/settings#tts", status_code=303)


@web_router.post("/settings/scheduler")
def web_update_scheduler(
    request: Request,
    db: Session = Depends(get_db),
    slots: List[str] = Form(...),
):
    """Actualiza los slots del pipeline scheduler desde la UI."""
    from app.scheduler.jobs import reschedule_all_pipeline_jobs

    validated_slots: list[str] = []
    for slot in slots:
        parts = slot.strip().split(":")
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail=f"Formato inválido: {slot!r} (HH:MM)")
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Formato inválido: {slot!r} (HH:MM)")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise HTTPException(status_code=400, detail=f"Hora o minuto fuera de rango: {slot!r}")
        validated_slots.append(f"{h:02d}:{m:02d}")

    if not validated_slots:
        raise HTTPException(status_code=400, detail="Se requiere al menos un slot")

    save_pipeline_slots(db, validated_slots)
    db.commit()

    s = request.app.state.settings
    s.daily_fetch_time = validated_slots[0]

    scheduler = request.app.state.scheduler
    reschedule_all_pipeline_jobs(
        scheduler,
        validated_slots,
        s.session_factory if hasattr(s, "session_factory") else request.app.state.session_factory,
        s.sources_config_path,
        s.audio_dir,
    )

    return RedirectResponse("/web/settings", status_code=303)
