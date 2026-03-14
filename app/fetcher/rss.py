"""
Fetcher de fuentes RSS/Atom.

Usa feedparser para parsear feeds y httpx.AsyncClient para detectar
redirects o feeds que necesiten headers personalizados.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

import feedparser
import httpx

logger = logging.getLogger(__name__)


# ─── Modelos de datos internos ─────────────────────────────────────────────────

class FetchedArticle:
    """Artículo crudo tal como viene del feed, antes de persistir."""

    __slots__ = (
        "title", "url", "summary", "content",
        "published_at", "guid", "source_url",
    )

    def __init__(
        self,
        title: str,
        url: str,
        guid: str,
        source_url: str,
        summary: Optional[str] = None,
        content: Optional[str] = None,
        published_at: Optional[datetime] = None,
    ) -> None:
        self.title = title
        self.url = url
        self.guid = guid
        self.source_url = source_url
        self.summary = summary
        self.content = content
        self.published_at = published_at

    def __repr__(self) -> str:
        return f"<FetchedArticle title={self.title[:50]!r}>"


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _struct_time_to_datetime(st: time.struct_time) -> datetime:
    """Convierte time.struct_time (UTC) → datetime con tzinfo=UTC."""
    timestamp = calendar.timegm(st)          # interpreta struct_time como UTC
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _parse_entry(entry, source_url: str) -> Optional[FetchedArticle]:
    """
    Extrae campos relevantes de una entrada de feedparser.
    Retorna None si no hay título o URL (campos mínimos requeridos).
    """
    title: str = getattr(entry, "title", "").strip()
    url: str = getattr(entry, "link", "").strip()

    if not title or not url:
        return None

    # GUID: usar entry.id si existe, sino la URL
    guid: str = getattr(entry, "id", url).strip() or url

    # Resumen: summary > description > None
    summary: Optional[str] = None
    raw_summary = getattr(entry, "summary", None) or getattr(entry, "description", None)
    if raw_summary:
        summary = raw_summary.strip() or None

    # Contenido completo (si el feed lo provee)
    content: Optional[str] = None
    if hasattr(entry, "content") and entry.content:
        content = entry.content[0].get("value", "").strip() or None

    # Fecha de publicación
    published_at: Optional[datetime] = None
    parsed_date = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed_date:
        try:
            published_at = _struct_time_to_datetime(parsed_date)
        except (OSError, OverflowError, ValueError) as exc:
            logger.debug("No se pudo convertir fecha: %s — %s", parsed_date, exc)

    return FetchedArticle(
        title=title,
        url=url,
        guid=guid,
        source_url=source_url,
        summary=summary,
        content=content,
        published_at=published_at,
    )


# ─── Fetcher principal ─────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)
DEFAULT_HEADERS = {
    "User-Agent": "daily-news/1.0 (RSS reader; +https://github.com/jsalvag/daily-news)"
}


async def fetch_feed(
    url: str,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    max_entries: int = 50,
) -> List[FetchedArticle]:
    """
    Descarga y parsea un feed RSS/Atom desde `url`.

    Estrategia:
    1. httpx descarga el contenido (maneja redirects, headers, TLS).
    2. feedparser parsea el XML/Atom/JSON desde el string descargado.

    Args:
        url:          URL del feed.
        timeout:      Configuración de timeouts de httpx.
        max_entries:  Máximo de artículos a retornar (los más recientes primero).

    Returns:
        Lista de FetchedArticle, vacía si hubo error o el feed no tiene entradas.
    """
    raw_content: Optional[bytes] = None

    try:
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            raw_content = response.content

    except httpx.TimeoutException:
        logger.warning("Timeout al obtener feed: %s", url)
        return []
    except httpx.HTTPStatusError as exc:
        logger.warning("HTTP %s al obtener feed: %s", exc.response.status_code, url)
        return []
    except httpx.RequestError as exc:
        logger.warning("Error de red al obtener feed %s: %s", url, exc)
        return []

    # feedparser parsea desde bytes (detecta encoding automáticamente)
    # Corremos en executor para no bloquear el event loop (feedparser es síncrono)
    loop = asyncio.get_running_loop()
    parsed = await loop.run_in_executor(None, feedparser.parse, raw_content)

    if parsed.bozo and not parsed.entries:
        logger.warning("Feed malformado o vacío: %s (bozo=%s)", url, parsed.bozo_exception)
        return []

    articles: List[FetchedArticle] = []
    for entry in parsed.entries[:max_entries]:
        article = _parse_entry(entry, source_url=url)
        if article is not None:
            articles.append(article)

    logger.info("Fetched %d artículos de %s", len(articles), url)
    return articles


async def fetch_feeds_concurrently(
    urls: List[str],
    max_concurrent: int = 5,
    **kwargs,
) -> dict[str, List[FetchedArticle]]:
    """
    Descarga múltiples feeds de forma concurrente con un semáforo.

    Args:
        urls:           Lista de URLs a descargar.
        max_concurrent: Máximo de descargas simultáneas.
        **kwargs:       Se pasan a `fetch_feed`.

    Returns:
        Dict {url: [FetchedArticle, ...]}
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _bounded_fetch(url: str) -> tuple[str, List[FetchedArticle]]:
        async with semaphore:
            articles = await fetch_feed(url, **kwargs)
            return url, articles

    tasks = [_bounded_fetch(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output: dict[str, List[FetchedArticle]] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error("Error inesperado en fetch concurrente: %s", result)
            continue
        url, articles = result
        output[url] = articles

    return output
