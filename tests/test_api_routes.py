"""
Tests de integración para los endpoints REST de la API.

Estrategia:
  - Se usa TestClient(app) SIN context manager para evitar ejecutar el lifespan
    (que requiere .env, Anthropic API, scheduler, etc.).
  - El fixture `api_client` configura manualmente app.state con objetos mock/reales
    y sobreescribe la dependencia get_db para usar una BD SQLite en memoria.
  - Los tests que necesitan datos en la BD los añaden directamente mediante
    el fixture `db_session`, que comparte el mismo engine que el api_client.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.api.routes import get_db
from app.main import app
from app.storage.models import (
    Article,
    DailyBriefing,
    Source,
    get_session_factory,
    init_db,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def db_engine():
    """
    Engine SQLite en memoria con StaticPool.

    StaticPool garantiza que todas las conexiones (incluidas las del worker
    thread de FastAPI TestClient) usen la MISMA conexión subyacente.
    Sin esto, cada hilo abre una conexión fresca que no ve las tablas del setup.
    """
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
    """Sesión de BD para insertar datos de prueba directamente."""
    SessionLocal = get_session_factory(db_engine)
    db = SessionLocal()
    yield db
    db.close()


@pytest.fixture
def api_client(db_engine):
    """
    TestClient configurado para tests de API:
      - Sin lifespan (no conecta a Anthropic, no inicia scheduler real).
      - BD SQLite en memoria compartida con db_session.
      - app.state poblado con mocks.
    """
    SessionLocal = get_session_factory(db_engine)

    # Settings mock — mismo interfaz que app.config.Settings
    mock_settings = MagicMock()
    mock_settings.sources_config_path = "config/sources.yaml"
    mock_settings.daily_fetch_time = "06:00"
    mock_settings.feed_title = "Test Daily News"
    mock_settings.feed_description = "Test feed"
    mock_settings.feed_base_url = "http://localhost:8000"

    app.state.session_factory = SessionLocal
    app.state.scheduler = MagicMock()
    app.state.settings = mock_settings

    # Sobreescribir get_db para que use el engine de test
    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    # Sin 'with' → no se ejecuta el lifespan
    client = TestClient(app, raise_server_exceptions=True)
    yield client

    app.dependency_overrides.clear()


@pytest.fixture
def source(db_session) -> Source:
    """Fuente de prueba insertada en la BD."""
    src = Source(
        name="Test News",
        url="https://test.com/rss",
        category="tecnologia",
        language="es",
        enabled=True,
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)
    return src


@pytest.fixture
def briefing(db_session) -> DailyBriefing:
    """Briefing de prueba insertado en la BD."""
    b = DailyBriefing(
        date="2024-01-15",
        headlines_text="1. Titular uno  2. Titular dos",
        full_text="• Titular uno\n  Resumen uno\n\n• Titular dos\n  Resumen dos",
        article_ids="1,2",
    )
    db_session.add(b)
    db_session.commit()
    db_session.refresh(b)
    return b


# ─── Tests: health ────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_retorna_ok(self, api_client):
        r = api_client.get("/api/v1/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_incluye_timestamp(self, api_client):
        r = api_client.get("/api/v1/health")
        assert "timestamp" in r.json()


# ─── Tests: sources (lista y detalle) ─────────────────────────────────────────

class TestListSources:
    def test_lista_vacia_inicial(self, api_client):
        r = api_client.get("/api/v1/sources")
        assert r.status_code == 200
        assert r.json() == []

    def test_lista_una_fuente(self, api_client, source):
        r = api_client.get("/api/v1/sources")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["name"] == "Test News"
        assert data[0]["url"] == "https://test.com/rss"

    def test_enabled_only_filtra_deshabilitadas(self, api_client, db_session):
        # Fuente habilitada
        db_session.add(Source(name="On", url="https://on.com/rss", category="tech", language="es", enabled=True))
        # Fuente deshabilitada
        db_session.add(Source(name="Off", url="https://off.com/rss", category="tech", language="es", enabled=False))
        db_session.commit()

        r = api_client.get("/api/v1/sources?enabled_only=true")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert "On" in names
        assert "Off" not in names

    def test_get_source_por_id(self, api_client, source):
        r = api_client.get(f"/api/v1/sources/{source.id}")
        assert r.status_code == 200
        assert r.json()["id"] == source.id

    def test_get_source_inexistente_404(self, api_client):
        r = api_client.get("/api/v1/sources/9999")
        assert r.status_code == 404


# ─── Tests: sources (toggle y delete) ─────────────────────────────────────────

class TestToggleSource:
    def test_deshabilita_fuente(self, api_client, source):
        r = api_client.patch(
            f"/api/v1/sources/{source.id}/toggle",
            json={"enabled": False},
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    def test_habilita_fuente(self, api_client, db_session):
        src = Source(name="X", url="https://x.com/rss", category="tech", language="es", enabled=False)
        db_session.add(src)
        db_session.commit()
        db_session.refresh(src)

        r = api_client.patch(f"/api/v1/sources/{src.id}/toggle", json={"enabled": True})
        assert r.status_code == 200
        assert r.json()["enabled"] is True

    def test_toggle_inexistente_404(self, api_client):
        r = api_client.patch("/api/v1/sources/9999/toggle", json={"enabled": False})
        assert r.status_code == 404


class TestDeleteSource:
    def test_elimina_fuente(self, api_client, source):
        r = api_client.delete(f"/api/v1/sources/{source.id}")
        assert r.status_code == 204

        r2 = api_client.get(f"/api/v1/sources/{source.id}")
        assert r2.status_code == 404

    def test_elimina_inexistente_404(self, api_client):
        r = api_client.delete("/api/v1/sources/9999")
        assert r.status_code == 404


# ─── Tests: sources sync ──────────────────────────────────────────────────────

class TestSyncSources:
    def test_sync_archivo_inexistente_404(self, api_client):
        """Si el YAML no existe, retorna 404."""
        # Apuntar a una ruta que definitivamente no existe
        app.state.settings.sources_config_path = "/tmp/no-existe-jamas/sources.yaml"
        r = api_client.post("/api/v1/sources/sync")
        # Restaurar
        app.state.settings.sources_config_path = "config/sources.yaml"
        assert r.status_code == 404


# ─── Tests: articles ──────────────────────────────────────────────────────────

class TestListArticles:
    def test_lista_vacia(self, api_client):
        r = api_client.get("/api/v1/articles")
        assert r.status_code == 200
        assert r.json() == []

    def test_fecha_invalida_400(self, api_client):
        r = api_client.get("/api/v1/articles?date_str=not-a-date")
        assert r.status_code == 400

    def test_filtra_por_fecha(self, api_client, db_session, source):
        today = date.today()
        db_session.add(Article(
            source_id=source.id,
            title="Artículo hoy",
            url="https://test.com/1",
            guid="g1",
            published_at=datetime(today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc),
            processed=True,
        ))
        db_session.commit()

        r = api_client.get(f"/api/v1/articles?date_str={today.isoformat()}")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_processed_only_filtra(self, api_client, db_session, source):
        today = date.today()
        db_session.add(Article(
            source_id=source.id,
            title="Procesado",
            url="https://test.com/proc",
            guid="g-proc",
            published_at=datetime(today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc),
            processed=True,
        ))
        db_session.add(Article(
            source_id=source.id,
            title="Sin procesar",
            url="https://test.com/unproc",
            guid="g-unproc",
            published_at=datetime(today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc),
            processed=False,
        ))
        db_session.commit()

        r = api_client.get(f"/api/v1/articles?date_str={today.isoformat()}&processed_only=true")
        assert r.status_code == 200
        titles = [a["title"] for a in r.json()]
        assert "Procesado" in titles
        assert "Sin procesar" not in titles


# ─── Tests: briefings ─────────────────────────────────────────────────────────

class TestBriefings:
    def test_latest_sin_datos_404(self, api_client):
        r = api_client.get("/api/v1/briefings/latest")
        assert r.status_code == 404

    def test_today_sin_datos_404(self, api_client):
        r = api_client.get("/api/v1/briefings/today")
        assert r.status_code == 404

    def test_fecha_invalida_400(self, api_client):
        r = api_client.get("/api/v1/briefings/no-es-fecha")
        assert r.status_code == 400

    def test_fecha_inexistente_404(self, api_client):
        r = api_client.get("/api/v1/briefings/2024-01-01")
        assert r.status_code == 404

    def test_get_briefing_por_fecha(self, api_client, briefing):
        r = api_client.get(f"/api/v1/briefings/{briefing.date}")
        assert r.status_code == 200
        data = r.json()
        assert data["date"] == "2024-01-15"
        assert "Titular uno" in data["headlines_text"]

    def test_latest_retorna_briefing(self, api_client, briefing):
        r = api_client.get("/api/v1/briefings/latest")
        assert r.status_code == 200
        assert r.json()["date"] == "2024-01-15"

    def test_today_retorna_briefing_de_hoy(self, api_client, db_session):
        today = date.today().strftime("%Y-%m-%d")
        b = DailyBriefing(
            date=today,
            headlines_text="1. Hoy",
            full_text="• Hoy\n  Resumen",
            article_ids="1",
        )
        db_session.add(b)
        db_session.commit()

        r = api_client.get("/api/v1/briefings/today")
        assert r.status_code == 200
        assert r.json()["date"] == today


# ─── Tests: jobs ──────────────────────────────────────────────────────────────

class TestJobs:
    def test_trigger_fetch_202(self, api_client):
        r = api_client.post("/api/v1/jobs/fetch")
        assert r.status_code == 202
        assert r.json()["status"] == "started"

    def test_trigger_process_202(self, api_client):
        r = api_client.post("/api/v1/jobs/process")
        assert r.status_code == 202
        assert r.json()["status"] == "started"

    def test_trigger_briefing_202(self, api_client):
        r = api_client.post("/api/v1/jobs/briefing")
        assert r.status_code == 202
        assert r.json()["status"] == "started"


# ─── Tests: settings ──────────────────────────────────────────────────────────

class TestSettings:
    def test_get_settings(self, api_client):
        r = api_client.get("/api/v1/settings")
        assert r.status_code == 200
        data = r.json()
        assert data["daily_fetch_time"] == "06:00"
        assert data["feed_title"] == "Test Daily News"

    def test_update_scheduler_tiempo_valido(self, api_client):
        r = api_client.patch(
            "/api/v1/settings/scheduler",
            json={"daily_fetch_time": "04:30"},
        )
        assert r.status_code == 200
        assert r.json()["daily_fetch_time"] == "04:30"

        # Verificar que se llamó a reschedule_job tres veces
        scheduler = app.state.scheduler
        assert scheduler.reschedule_job.call_count == 3

    def test_update_scheduler_tiempo_invalido_422(self, api_client):
        r = api_client.patch(
            "/api/v1/settings/scheduler",
            json={"daily_fetch_time": "25:00"},
        )
        assert r.status_code == 422

    def test_update_scheduler_formato_incorrecto_422(self, api_client):
        r = api_client.patch(
            "/api/v1/settings/scheduler",
            json={"daily_fetch_time": "not-a-time"},
        )
        assert r.status_code == 422
