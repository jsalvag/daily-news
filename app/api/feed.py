"""
Feed Atom para Google Home y otros dispositivos de voz.

Expone los DailyBriefings como un feed Atom estándar:
  - headlines_text → <summary>   (texto breve, optimizado para TTS)
  - full_text      → <content>   (versión completa para iPhone / lectores)

El feed se sirve en GET /feed.xml y acepta un parámetro ?limit=N (default 7).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.routes import get_db
from app.storage.crud import get_recent_briefings
from app.storage.models import DailyBriefing

feed_router = APIRouter()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _briefing_dt(briefing: DailyBriefing) -> datetime:
    """
    Devuelve un datetime UTC para el briefing.

    Usa generated_at si está disponible; si no, construye uno desde el campo
    date (YYYY-MM-DD) a medianoche UTC.
    """
    if briefing.generated_at is not None:
        dt = briefing.generated_at
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    return datetime.strptime(briefing.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)


# ─── Generador de XML ─────────────────────────────────────────────────────────

def generate_atom_feed(
    briefings: list[DailyBriefing],
    *,
    feed_id: str,
    feed_title: str,
    feed_description: str,
    feed_base_url: str,
) -> bytes:
    """
    Genera el XML Atom a partir de una lista de DailyBriefings.

    Args:
        briefings:        Lista ordenada del más reciente al más antiguo.
        feed_id:          URL canónica del feed (usada como <id>).
        feed_title:       Título del feed.
        feed_description: Subtítulo / descripción corta.
        feed_base_url:    URL base del servidor (para construir los links de cada entrada).

    Returns:
        Bytes UTF-8 del documento Atom XML.
    """
    from feedgen.feed import FeedGenerator

    fg = FeedGenerator()
    fg.id(feed_id)
    fg.title(feed_title)
    fg.subtitle(feed_description)
    fg.author({"name": "daily-news", "email": "noreply@daily-news.local"})
    fg.link(href=feed_base_url, rel="alternate")
    fg.link(href=feed_id, rel="self")
    fg.language("es")

    # La fecha de actualización del feed es la del briefing más reciente
    if briefings:
        fg.updated(_briefing_dt(briefings[0]))
    else:
        fg.updated(datetime.now(tz=timezone.utc))

    for briefing in briefings:
        fe = fg.add_entry(order="append")  # orden: más reciente primero

        entry_url = f"{feed_base_url.rstrip('/')}/api/v1/briefings/{briefing.date}"
        fe.id(entry_url)
        fe.title(f"Briefing del {briefing.date}")
        fe.link(href=entry_url)

        dt = _briefing_dt(briefing)
        fe.published(dt)
        fe.updated(dt)

        # summary = texto corto (TTS, Google Home)
        if briefing.headlines_text:
            fe.summary(briefing.headlines_text)

        # content = texto completo (iPhone, lectores RSS)
        if briefing.full_text:
            fe.content(briefing.full_text, type="text")

    return fg.atom_str(pretty=True)


# ─── Endpoint ─────────────────────────────────────────────────────────────────

@feed_router.get(
    "/feed.xml",
    response_class=Response,
    tags=["feed"],
    summary="Feed Atom con los últimos briefings diarios",
    description=(
        "Devuelve un feed Atom con los últimos N briefings (por defecto 7). "
        "Cada entrada contiene un `<summary>` con el texto breve optimizado para TTS "
        "y un `<content>` con el resumen completo."
    ),
)
def get_atom_feed(
    request: Request,
    limit: int = 7,
    db: Session = Depends(get_db),
) -> Response:
    """Feed Atom con los últimos briefings diarios."""
    settings = request.app.state.settings
    briefings = get_recent_briefings(db, limit=limit)

    feed_id = f"{settings.feed_base_url.rstrip('/')}/feed.xml"
    xml = generate_atom_feed(
        briefings,
        feed_id=feed_id,
        feed_title=settings.feed_title,
        feed_description=settings.feed_description,
        feed_base_url=settings.feed_base_url,
    )
    return Response(content=xml, media_type="application/atom+xml; charset=utf-8")
