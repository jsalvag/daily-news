"""
Tests para el fetcher de RSS.
Usa pytest-httpx para mockear llamadas HTTP sin red real.
"""

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.fetcher.rss import (
    FetchedArticle,
    _parse_entry,
    _struct_time_to_datetime,
    fetch_feed,
    fetch_feeds_concurrently,
)

# ─── Feed RSS de prueba ───────────────────────────────────────────────────────

SAMPLE_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <link>https://example.com</link>
  <description>Feed de prueba</description>

  <item>
    <title>Noticia uno</title>
    <link>https://example.com/nota/1</link>
    <description>Resumen de la noticia uno</description>
    <guid>https://example.com/nota/1</guid>
    <pubDate>Mon, 13 Jan 2025 10:00:00 GMT</pubDate>
  </item>

  <item>
    <title>Noticia dos</title>
    <link>https://example.com/nota/2</link>
    <description>Resumen de la noticia dos</description>
    <guid>guid-nota-2</guid>
    <pubDate>Mon, 13 Jan 2025 12:00:00 GMT</pubDate>
  </item>

  <item>
    <title>   </title>
    <link></link>
    <description>Entrada sin título ni URL — debe descartarse</description>
  </item>
</channel>
</rss>""".encode("utf-8")

EMPTY_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Vacío</title></channel></rss>""".encode("utf-8")


# ─── Tests: _struct_time_to_datetime ─────────────────────────────────────────

class TestStructTimeToDatetime:
    def test_convierte_correctamente(self):
        st = time.strptime("2025-01-13 10:00:00", "%Y-%m-%d %H:%M:%S")
        dt = _struct_time_to_datetime(st)
        assert isinstance(dt, datetime)
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 13
        assert dt.hour == 10

    def test_resultado_con_timezone_utc(self):
        st = time.gmtime(0)          # epoch
        dt = _struct_time_to_datetime(st)
        assert dt == datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ─── Tests: _parse_entry ─────────────────────────────────────────────────────

class TestParseEntry:
    def _make_entry(self, **kwargs):
        """Crea un objeto simple que simula una entrada de feedparser."""
        class MockEntry:
            pass
        e = MockEntry()
        defaults = {
            "title": "Título de prueba",
            "link": "https://example.com/nota",
            "id": "guid-123",
            "summary": "Resumen de prueba",
            "published_parsed": time.strptime("2025-01-13", "%Y-%m-%d"),
        }
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(e, k, v)
        return e

    def test_parsea_entry_completa(self):
        entry = self._make_entry()
        result = _parse_entry(entry, source_url="https://example.com/rss")
        assert result is not None
        assert result.title == "Título de prueba"
        assert result.url == "https://example.com/nota"
        assert result.guid == "guid-123"
        assert result.summary == "Resumen de prueba"
        assert result.published_at is not None
        assert result.published_at.year == 2025

    def test_sin_titulo_retorna_none(self):
        entry = self._make_entry(title="")
        assert _parse_entry(entry, source_url="x") is None

    def test_sin_url_retorna_none(self):
        entry = self._make_entry(link="")
        assert _parse_entry(entry, source_url="x") is None

    def test_guid_fallback_a_url(self):
        """Si no hay id, el guid debe ser la URL."""
        entry = self._make_entry()
        del entry.id                  # eliminar atributo id
        result = _parse_entry(entry, source_url="x")
        assert result is not None
        assert result.guid == result.url

    def test_sin_fecha_published_at_es_none(self):
        entry = self._make_entry(published_parsed=None)
        result = _parse_entry(entry, source_url="x")
        assert result is not None
        assert result.published_at is None

    def test_summary_vacio_queda_none(self):
        entry = self._make_entry(summary="   ")
        result = _parse_entry(entry, source_url="x")
        assert result is not None
        assert result.summary is None


# ─── Tests: fetch_feed ───────────────────────────────────────────────────────

class TestFetchFeed:
    @pytest.mark.asyncio
    async def test_parsea_feed_valido(self, httpx_mock):
        """Fetch exitoso: retorna lista de artículos."""
        httpx_mock.add_response(
            url="https://example.com/feed.rss",
            content=SAMPLE_RSS,
            status_code=200,
        )
        articles = await fetch_feed("https://example.com/feed.rss")

        # La entrada sin título/URL debe descartarse → solo 2 artículos
        assert len(articles) == 2
        assert all(isinstance(a, FetchedArticle) for a in articles)
        assert articles[0].title == "Noticia uno"
        assert articles[1].guid == "guid-nota-2"

    @pytest.mark.asyncio
    async def test_feed_vacio_retorna_lista_vacia(self, httpx_mock):
        httpx_mock.add_response(
            url="https://example.com/empty.rss",
            content=EMPTY_RSS,
            status_code=200,
        )
        articles = await fetch_feed("https://example.com/empty.rss")
        assert articles == []

    @pytest.mark.asyncio
    async def test_http_404_retorna_lista_vacia(self, httpx_mock):
        httpx_mock.add_response(
            url="https://example.com/notfound.rss",
            status_code=404,
        )
        articles = await fetch_feed("https://example.com/notfound.rss")
        assert articles == []

    @pytest.mark.asyncio
    async def test_timeout_retorna_lista_vacia(self, httpx_mock):
        import httpx as _httpx
        httpx_mock.add_exception(
            _httpx.ReadTimeout("timeout"),
            url="https://example.com/slow.rss",
        )
        articles = await fetch_feed("https://example.com/slow.rss")
        assert articles == []

    @pytest.mark.asyncio
    async def test_max_entries_limita_resultados(self, httpx_mock):
        """max_entries=1 debe retornar solo el primer artículo."""
        httpx_mock.add_response(
            url="https://example.com/feed.rss",
            content=SAMPLE_RSS,
            status_code=200,
        )
        articles = await fetch_feed("https://example.com/feed.rss", max_entries=1)
        assert len(articles) == 1

    @pytest.mark.asyncio
    async def test_published_at_es_datetime_utc(self, httpx_mock):
        httpx_mock.add_response(
            url="https://example.com/feed.rss",
            content=SAMPLE_RSS,
            status_code=200,
        )
        articles = await fetch_feed("https://example.com/feed.rss")
        for a in articles:
            if a.published_at:
                assert a.published_at.tzinfo == timezone.utc


# ─── Tests: fetch_feeds_concurrently ─────────────────────────────────────────

class TestFetchFeedsConcurrently:
    @pytest.mark.asyncio
    async def test_multiples_urls(self, httpx_mock):
        for i in range(1, 4):
            httpx_mock.add_response(
                url=f"https://example.com/feed{i}.rss",
                content=SAMPLE_RSS,
                status_code=200,
            )
        urls = [f"https://example.com/feed{i}.rss" for i in range(1, 4)]
        result = await fetch_feeds_concurrently(urls, max_concurrent=3)

        assert set(result.keys()) == set(urls)
        for url, articles in result.items():
            assert len(articles) == 2     # 2 artículos válidos por feed

    @pytest.mark.asyncio
    async def test_algunas_urls_fallidas(self, httpx_mock):
        httpx_mock.add_response(
            url="https://example.com/ok.rss",
            content=SAMPLE_RSS,
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://example.com/fail.rss",
            status_code=500,
        )
        result = await fetch_feeds_concurrently(
            ["https://example.com/ok.rss", "https://example.com/fail.rss"]
        )
        assert len(result["https://example.com/ok.rss"]) == 2
        assert result["https://example.com/fail.rss"] == []
