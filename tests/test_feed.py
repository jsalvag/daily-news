"""
Tests para el endpoint de feed Atom (/feed.xml) y su generador.

Estrategia:
  - TestGenerateAtomFeed: tests unitarios de generate_atom_feed (sin HTTP).
  - TestFeedEndpoint: tests de integración que usan TestClient con la misma
    infraestructura de fixtures que test_api_routes.py.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.api.routes import get_db
from app.main import app
from app.storage.models import DailyBriefing, get_session_factory, init_db

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    SessionLocal = get_session_factory(db_engine)
    db = SessionLocal()
    yield db
    db.close()


@pytest.fixture
def api_client(db_engine):
    SessionLocal = get_session_factory(db_engine)

    mock_settings = MagicMock()
    mock_settings.feed_title = "Test Daily Briefing"
    mock_settings.feed_description = "Resumen diario de prueba"
    mock_settings.feed_base_url = "http://testserver"

    app.state.session_factory = SessionLocal
    app.state.anthropic_client = MagicMock()
    app.state.scheduler = MagicMock()
    app.state.settings = mock_settings

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app, raise_server_exceptions=True)
    yield client

    app.dependency_overrides.clear()


# ─── Helper ───────────────────────────────────────────────────────────────────

def _make_briefing(date_str: str, headlines: str, full: str) -> DailyBriefing:
    """Crea un DailyBriefing en memoria (sin session)."""
    b = DailyBriefing()
    b.date = date_str
    b.headlines_text = headlines
    b.full_text = full
    b.generated_at = datetime(2024, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
    return b


# ─── Tests: generate_atom_feed ────────────────────────────────────────────────

class TestGenerateAtomFeed:
    def _gen(self, briefings, **kwargs):
        from app.api.feed import generate_atom_feed
        defaults = {
            "feed_id": "http://localhost/feed.xml",
            "feed_title": "Test Feed",
            "feed_description": "Descripción",
            "feed_base_url": "http://localhost",
        }
        defaults.update(kwargs)
        return generate_atom_feed(briefings, **defaults)

    def test_retorna_bytes(self):
        xml = self._gen([])
        assert isinstance(xml, bytes)

    def test_xml_valido_y_parseable(self):
        xml = self._gen([_make_briefing("2024-01-15", "H", "F")])
        root = ET.fromstring(xml)  # lanza si no es XML válido
        assert root is not None

    def test_root_es_feed_atom(self):
        xml = self._gen([])
        root = ET.fromstring(xml)
        assert root.tag == "{http://www.w3.org/2005/Atom}feed"

    def test_feed_title_en_metadata(self):
        xml = self._gen([], feed_title="Mi Feed Especial")
        root = ET.fromstring(xml)
        title = root.find("atom:title", ATOM_NS)
        assert title is not None
        assert title.text == "Mi Feed Especial"

    def test_feed_vacio_sin_entries(self):
        xml = self._gen([])
        root = ET.fromstring(xml)
        entries = root.findall("atom:entry", ATOM_NS)
        assert len(entries) == 0

    def test_un_entry_por_briefing(self):
        briefings = [
            _make_briefing("2024-01-15", "H1", "F1"),
            _make_briefing("2024-01-14", "H2", "F2"),
            _make_briefing("2024-01-13", "H3", "F3"),
        ]
        xml = self._gen(briefings)
        root = ET.fromstring(xml)
        entries = root.findall("atom:entry", ATOM_NS)
        assert len(entries) == 3

    def test_summary_es_headlines_text(self):
        xml = self._gen([_make_briefing("2024-01-15", "1. Noticia A  2. Noticia B", "Detalle")])
        root = ET.fromstring(xml)
        entry = root.find("atom:entry", ATOM_NS)
        summary = entry.find("atom:summary", ATOM_NS)
        assert summary is not None
        assert "Noticia A" in summary.text

    def test_content_es_full_text(self):
        xml = self._gen([_make_briefing("2024-01-15", "H", "• Noticia\n  Resumen largo")])
        root = ET.fromstring(xml)
        entry = root.find("atom:entry", ATOM_NS)
        content = entry.find("atom:content", ATOM_NS)
        assert content is not None
        assert "Resumen largo" in content.text

    def test_entry_id_contiene_fecha(self):
        xml = self._gen([_make_briefing("2024-01-15", "H", "F")])
        root = ET.fromstring(xml)
        entry = root.find("atom:entry", ATOM_NS)
        entry_id = entry.find("atom:id", ATOM_NS)
        assert "2024-01-15" in entry_id.text

    def test_entry_id_contiene_base_url(self):
        xml = self._gen(
            [_make_briefing("2024-01-15", "H", "F")],
            feed_base_url="http://mi-servidor",
        )
        root = ET.fromstring(xml)
        entry = root.find("atom:entry", ATOM_NS)
        entry_id = entry.find("atom:id", ATOM_NS)
        assert "mi-servidor" in entry_id.text

    def test_entry_tiene_published(self):
        xml = self._gen([_make_briefing("2024-01-15", "H", "F")])
        root = ET.fromstring(xml)
        entry = root.find("atom:entry", ATOM_NS)
        published = entry.find("atom:published", ATOM_NS)
        assert published is not None
        assert published.text  # no vacío

    def test_briefing_sin_generated_at_usa_fecha(self):
        """Si generated_at es None, la fecha del campo date se usa como fallback."""
        b = DailyBriefing()
        b.date = "2024-01-10"
        b.headlines_text = "H"
        b.full_text = "F"
        b.generated_at = None

        xml = self._gen([b])
        root = ET.fromstring(xml)
        entry = root.find("atom:entry", ATOM_NS)
        assert entry is not None  # no lanzó excepción

    def test_feed_link_self_apunta_a_feed_id(self):
        xml = self._gen([], feed_id="http://ejemplo.com/feed.xml")
        root = ET.fromstring(xml)
        links = root.findall("atom:link", ATOM_NS)
        self_links = [l for l in links if l.get("rel") == "self"]
        assert len(self_links) == 1
        assert self_links[0].get("href") == "http://ejemplo.com/feed.xml"

    def test_multiples_briefings_orden_preservado(self):
        """El primer entry del XML debe corresponder al briefing más reciente."""
        briefings = [
            _make_briefing("2024-01-15", "Hoy", "F1"),
            _make_briefing("2024-01-14", "Ayer", "F2"),
        ]
        xml = self._gen(briefings)
        root = ET.fromstring(xml)
        entries = root.findall("atom:entry", ATOM_NS)
        # El primero debe ser el más reciente (2024-01-15)
        first_id = entries[0].find("atom:id", ATOM_NS).text
        assert "2024-01-15" in first_id


# ─── Tests: GET /feed.xml ─────────────────────────────────────────────────────

class TestFeedEndpoint:
    def test_retorna_200(self, api_client):
        r = api_client.get("/feed.xml")
        assert r.status_code == 200

    def test_content_type_es_atom(self, api_client):
        r = api_client.get("/feed.xml")
        assert "atom+xml" in r.headers["content-type"]

    def test_feed_vacio_es_xml_valido(self, api_client):
        r = api_client.get("/feed.xml")
        root = ET.fromstring(r.content)
        assert root.tag == "{http://www.w3.org/2005/Atom}feed"

    def test_feed_con_briefings(self, api_client, db_session):
        for date_str in ["2024-01-15", "2024-01-14", "2024-01-13"]:
            db_session.add(DailyBriefing(
                date=date_str,
                headlines_text=f"Titular del {date_str}",
                full_text=f"Detalle del {date_str}",
                article_ids="1,2",
            ))
        db_session.commit()

        r = api_client.get("/feed.xml")
        assert r.status_code == 200
        root = ET.fromstring(r.content)
        entries = root.findall("atom:entry", ATOM_NS)
        assert len(entries) == 3

    def test_limit_restringe_entradas(self, api_client, db_session):
        for i in range(1, 10):
            db_session.add(DailyBriefing(
                date=f"2024-01-{i:02d}",
                headlines_text=f"H{i}",
                full_text=f"F{i}",
                article_ids="1",
            ))
        db_session.commit()

        r = api_client.get("/feed.xml?limit=3")
        assert r.status_code == 200
        root = ET.fromstring(r.content)
        entries = root.findall("atom:entry", ATOM_NS)
        assert len(entries) == 3

    def test_default_limit_es_7(self, api_client, db_session):
        for i in range(1, 11):    # 10 briefings
            db_session.add(DailyBriefing(
                date=f"2024-01-{i:02d}",
                headlines_text=f"H{i}",
                full_text=f"F{i}",
                article_ids="1",
            ))
        db_session.commit()

        r = api_client.get("/feed.xml")
        assert r.status_code == 200
        root = ET.fromstring(r.content)
        entries = root.findall("atom:entry", ATOM_NS)
        assert len(entries) == 7

    def test_feed_title_en_xml(self, api_client):
        r = api_client.get("/feed.xml")
        root = ET.fromstring(r.content)
        title = root.find("atom:title", ATOM_NS)
        assert title is not None
        assert title.text == "Test Daily Briefing"

    def test_entry_summary_contiene_headlines(self, api_client, db_session):
        db_session.add(DailyBriefing(
            date="2024-01-15",
            headlines_text="1. Noticia importante  2. Otra noticia",
            full_text="Detalle completo de las noticias",
            article_ids="1,2",
        ))
        db_session.commit()

        r = api_client.get("/feed.xml")
        root = ET.fromstring(r.content)
        entry = root.find("atom:entry", ATOM_NS)
        summary = entry.find("atom:summary", ATOM_NS)
        assert summary is not None
        assert "Noticia importante" in summary.text

    def test_entry_content_contiene_full_text(self, api_client, db_session):
        db_session.add(DailyBriefing(
            date="2024-01-15",
            headlines_text="H",
            full_text="• Artículo uno\n  Resumen completo del artículo",
            article_ids="1",
        ))
        db_session.commit()

        r = api_client.get("/feed.xml")
        root = ET.fromstring(r.content)
        entry = root.find("atom:entry", ATOM_NS)
        content = entry.find("atom:content", ATOM_NS)
        assert content is not None
        assert "Resumen completo" in content.text
