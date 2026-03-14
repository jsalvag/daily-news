"""
Pruebas para la capa de almacenamiento (modelos y base de datos).
Usa SQLite en memoria para no tocar el archivo real.
"""

import pytest
from datetime import datetime
from sqlalchemy import inspect

from app.storage.models import (
    Base,
    Source,
    Article,
    DailyBriefing,
    create_db_engine,
    init_db,
    get_session_factory,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    """Engine SQLite en memoria para cada test."""
    eng = create_db_engine("sqlite:///:memory:")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """Sesión de BD limpia para cada test."""
    Session = get_session_factory(engine)
    db = Session()
    yield db
    db.close()


# ─── Tests: init_db ───────────────────────────────────────────────────────────

class TestInitDb:
    def test_creates_all_tables(self, engine):
        """Las tres tablas deben existir tras init_db."""
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        assert "sources" in tables
        assert "articles" in tables
        assert "daily_briefings" in tables

    def test_idempotente(self, engine):
        """Llamar init_db dos veces no lanza errores."""
        init_db(engine)  # segunda vez
        inspector = inspect(engine)
        assert len(inspector.get_table_names()) == 3


# ─── Tests: Source ────────────────────────────────────────────────────────────

class TestSource:
    def test_crear_fuente(self, session):
        src = Source(
            name="Infobae",
            url="https://www.infobae.com/feeds/rss/",
            category="argentina",
        )
        session.add(src)
        session.commit()

        result = session.query(Source).filter_by(name="Infobae").first()
        assert result is not None
        assert result.category == "argentina"
        assert result.enabled is True          # valor por defecto
        assert result.source_type == "rss"     # valor por defecto

    def test_url_unica(self, session):
        """No se pueden duplicar URLs."""
        from sqlalchemy.exc import IntegrityError

        session.add(Source(name="A", url="https://dupe.com/rss", category="x"))
        session.commit()

        session.add(Source(name="B", url="https://dupe.com/rss", category="y"))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_deshabilitar_fuente(self, session):
        src = Source(
            name="Test", url="https://test.com/rss", category="test", enabled=True
        )
        session.add(src)
        session.commit()

        src.enabled = False
        session.commit()

        result = session.query(Source).filter_by(name="Test").first()
        assert result.enabled is False

    def test_repr(self, session):
        src = Source(name="La Nación", url="https://ln.com/rss", category="argentina")
        assert "La Nación" in repr(src)
        assert "argentina" in repr(src)


# ─── Tests: Article ───────────────────────────────────────────────────────────

class TestArticle:
    def _make_source(self, session) -> Source:
        src = Source(name="Src", url="https://src.com/rss", category="test")
        session.add(src)
        session.commit()
        return src

    def test_crear_articulo(self, session):
        src = self._make_source(session)
        art = Article(
            source_id=src.id,
            title="Argentina gana el mundial",
            url="https://src.com/nota/1",
            published_at=datetime(2024, 12, 1, 10, 0),
        )
        session.add(art)
        session.commit()

        result = session.query(Article).filter_by(source_id=src.id).first()
        assert result is not None
        assert result.title == "Argentina gana el mundial"
        assert result.processed is False       # valor por defecto

    def test_marcar_procesado(self, session):
        src = self._make_source(session)
        art = Article(
            source_id=src.id,
            title="Test",
            url="https://src.com/nota/2",
        )
        session.add(art)
        session.commit()

        art.processed = True
        art.ai_headline = "Titular corto"
        art.ai_summary = "Resumen generado por IA"
        art.relevance_score = 0.85
        session.commit()

        result = session.query(Article).get(art.id)
        assert result.processed is True
        assert result.relevance_score == 0.85

    def test_relacion_source_articles(self, session):
        src = self._make_source(session)
        for i in range(3):
            session.add(Article(
                source_id=src.id,
                title=f"Nota {i}",
                url=f"https://src.com/nota/{i}",
            ))
        session.commit()

        src_db = session.query(Source).get(src.id)
        assert len(src_db.articles) == 3

    def test_repr(self):
        art = Article(source_id=1, title="Un título largo que se trunca aquí", url="x")
        assert "Un título largo" in repr(art)


# ─── Tests: DailyBriefing ─────────────────────────────────────────────────────

class TestDailyBriefing:
    def test_crear_briefing(self, session):
        b = DailyBriefing(
            date="2024-12-01",
            headlines_text="Noticias del día...",
            full_text="Texto completo...",
            article_ids="1,2,3",
        )
        session.add(b)
        session.commit()

        result = session.query(DailyBriefing).filter_by(date="2024-12-01").first()
        assert result is not None
        assert result.article_ids == "1,2,3"

    def test_fecha_unica(self, session):
        """No puede haber dos briefings del mismo día."""
        from sqlalchemy.exc import IntegrityError

        session.add(DailyBriefing(date="2024-12-01", headlines_text="a"))
        session.commit()

        session.add(DailyBriefing(date="2024-12-01", headlines_text="b"))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_repr(self):
        b = DailyBriefing(date="2024-12-01")
        assert "2024-12-01" in repr(b)
