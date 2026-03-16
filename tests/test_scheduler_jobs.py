"""
Tests para el scheduler y la generación del briefing diario.

Los jobs de APScheduler se testean a nivel de la función interna (_job),
no del scheduler en sí — evitamos iniciar threads de background en tests.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from app.scheduler.jobs import (
    _parse_hhmm,
    create_scheduler,
    generate_daily_briefing,
    make_briefing_job,
    make_fetch_job,
    make_process_job,
)
from app.storage.crud import get_briefing_by_date
from app.storage.models import Article, DailyBriefing, Source, create_db_engine, get_session_factory, init_db


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def session():
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    Session = get_session_factory(engine)
    db = Session()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture
def session_factory(session):
    """Factory que siempre retorna la misma sesión de test."""
    return lambda: session


@pytest.fixture
def source(session) -> Source:
    src = Source(
        name="Test",
        url="https://test.com/rss",
        category="tecnologia",
        language="en",
        enabled=True,
    )
    session.add(src)
    session.commit()
    return src


def _processed_article(session, source, *, headline: str, score: float, day: date) -> Article:
    """Crea un artículo ya procesado con ai_headline y relevance_score."""
    a = Article(
        source_id=source.id,
        title=f"Título: {headline}",
        url=f"https://test.com/{headline.replace(' ', '-')}",
        guid=f"guid-{headline}",
        published_at=datetime(day.year, day.month, day.day, 10, 0, tzinfo=timezone.utc),
        ai_headline=headline,
        ai_summary=f"Resumen de {headline}.",
        relevance_score=score,
        processed=True,
    )
    session.add(a)
    return a


# ─── Tests: _parse_hhmm ───────────────────────────────────────────────────────

class TestParseHhmm:
    def test_parsea_formato_valido(self):
        assert _parse_hhmm("06:00") == (6, 0)
        assert _parse_hhmm("23:59") == (23, 59)
        assert _parse_hhmm("00:30") == (0, 30)

    def test_lanza_error_formato_invalido(self):
        with pytest.raises(ValueError):
            _parse_hhmm("6:00:00")
        with pytest.raises(ValueError):
            _parse_hhmm("06-00")
        with pytest.raises(ValueError):
            _parse_hhmm("not-a-time")


# ─── Tests: generate_daily_briefing ──────────────────────────────────────────

class TestGenerateDailyBriefing:
    def test_retorna_none_sin_articulos(self, session):
        result = generate_daily_briefing(session, date(2024, 1, 15))
        assert result is None

    def test_genera_briefing_con_articulos(self, session, source):
        day = date(2024, 1, 15)
        _processed_article(session, source, headline="Noticia uno", score=0.9, day=day)
        _processed_article(session, source, headline="Noticia dos", score=0.7, day=day)
        session.commit()

        briefing = generate_daily_briefing(session, day)
        session.commit()

        assert briefing is not None
        assert briefing.date == "2024-01-15"
        assert "Noticia uno" in briefing.headlines_text
        assert "Noticia dos" in briefing.headlines_text

    def test_headlines_son_numerados(self, session, source):
        day = date(2024, 1, 15)
        for i in range(3):
            _processed_article(session, source, headline=f"Titular {i}", score=0.5, day=day)
        session.commit()

        briefing = generate_daily_briefing(session, day)

        assert "1." in briefing.headlines_text
        assert "2." in briefing.headlines_text
        assert "3." in briefing.headlines_text

    def test_max_7_titulares_en_voz(self, session, source):
        day = date(2024, 1, 15)
        for i in range(10):
            _processed_article(session, source, headline=f"Art {i}", score=0.5, day=day)
        session.commit()

        briefing = generate_daily_briefing(session, day)

        # Solo 7 numeraciones (1. ... 7.) en headlines_text
        assert "7." in briefing.headlines_text
        assert "8." not in briefing.headlines_text

    def test_full_text_incluye_resumenes(self, session, source):
        day = date(2024, 1, 15)
        _processed_article(session, source, headline="Gran noticia", score=0.9, day=day)
        session.commit()

        briefing = generate_daily_briefing(session, day)

        assert "Gran noticia" in briefing.full_text
        assert "Resumen de Gran noticia." in briefing.full_text

    def test_article_ids_guardados_en_briefing(self, session, source):
        day = date(2024, 1, 15)
        a1 = _processed_article(session, source, headline="A1", score=0.9, day=day)
        a2 = _processed_article(session, source, headline="A2", score=0.5, day=day)
        session.commit()
        session.refresh(a1)
        session.refresh(a2)

        briefing = generate_daily_briefing(session, day)
        session.commit()

        ids_in_briefing = [int(x) for x in briefing.article_ids.split(",")]
        assert a1.id in ids_in_briefing
        assert a2.id in ids_in_briefing

    def test_usa_fecha_hoy_por_defecto(self, session, source):
        today = date.today()
        _processed_article(session, source, headline="Hoy", score=0.8, day=today)
        session.commit()

        briefing = generate_daily_briefing(session)  # sin target_date

        assert briefing is not None
        assert briefing.date == today.strftime("%Y-%m-%d")

    def test_solo_incluye_articulos_procesados(self, session, source):
        day = date(2024, 1, 15)
        # Artículo procesado
        _processed_article(session, source, headline="Procesado", score=0.9, day=day)
        # Artículo sin procesar
        session.add(Article(
            source_id=source.id,
            title="Sin procesar",
            url="https://test.com/sin-procesar",
            guid="guid-sin-procesar",
            published_at=datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc),
            processed=False,
        ))
        session.commit()

        briefing = generate_daily_briefing(session, day)

        assert "Procesado" in briefing.headlines_text
        assert "Sin procesar" not in briefing.headlines_text


# ─── Tests: make_fetch_job ────────────────────────────────────────────────────

class TestMakeFetchJob:
    def test_job_llama_a_fetch_y_guarda_articulos(self, session, source, session_factory):
        from app.fetcher.rss import FetchedArticle
        from datetime import timezone

        fetched = [
            FetchedArticle(
                title="Noticia de prueba",
                url="https://test.com/noticia",
                guid="guid-fetch-1",
                source_url=source.url,
                summary="Resumen",
                content=None,
                published_at=datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc),
            )
        ]

        with patch("app.scheduler.jobs.fetch_feeds_concurrently") as mock_fetch, \
             patch("app.scheduler.jobs.load_and_sync") as mock_sync:
            mock_fetch.return_value = {source.url: fetched}

            job = make_fetch_job(session_factory, "config/sources.yaml")
            job()
            session.commit()

        # Verificar que se intentó sincronizar el YAML
        mock_sync.assert_called_once()

        # Verificar que el artículo fue insertado
        articles = session.query(Article).all()
        assert len(articles) == 1
        assert articles[0].title == "Noticia de prueba"

    def test_job_no_falla_si_no_hay_fuentes(self, session, session_factory):
        """Si no hay fuentes habilitadas, el job termina silenciosamente."""
        # Deshabilitar todas las fuentes
        sources = session.query(Source).all()
        for s in sources:
            s.enabled = False
        session.commit()

        with patch("app.scheduler.jobs.load_and_sync"), \
             patch("app.scheduler.jobs.fetch_feeds_concurrently") as mock_fetch:
            job = make_fetch_job(session_factory, "config/sources.yaml")
            job()  # no debe lanzar excepción

        mock_fetch.assert_not_called()


# ─── Tests: make_process_job ──────────────────────────────────────────────────

class TestMakeProcessJob:
    def test_job_procesa_un_batch_hasta_vaciar_cola(self, session_factory):
        """El job llama a process_pending_articles una vez si hay un solo batch pendiente."""
        mock_cfg = MagicMock()
        mock_cfg.litellm_model = "groq/llama3-70b-8192"
        mock_cfg.api_key = "gsk_test"
        mock_cfg.base_url = None

        from app.processor.llm import ProcessResult

        with patch("app.scheduler.jobs.process_pending_articles") as mock_process, \
             patch("app.scheduler.jobs.get_model_config", return_value=mock_cfg), \
             patch("app.scheduler.jobs.count_articles", side_effect=[3, 0]):
            # side_effect: primera llamada → 3 pendientes, segunda → 0 (cola vacía)
            mock_process.return_value = ProcessResult(processed=3)

            job = make_process_job(session_factory)
            job()

        mock_process.assert_called_once()

    def test_job_procesa_multiples_batches(self, session_factory):
        """Con más artículos que el batch_size, el job itera hasta vaciar la cola."""
        mock_cfg = MagicMock()
        mock_cfg.litellm_model = "groq/llama3-70b-8192"
        mock_cfg.api_key = "gsk_test"
        mock_cfg.base_url = None

        from app.processor.llm import ProcessResult

        with patch("app.scheduler.jobs.process_pending_articles") as mock_process, \
             patch("app.scheduler.jobs.get_model_config", return_value=mock_cfg), \
             patch("app.scheduler.jobs.count_articles", side_effect=[80, 30, 30, 0]), \
             patch("app.scheduler.jobs.time.sleep"):
            # 80 pendientes → batch 1 → 30 quedan → batch 2 → 0 quedan
            mock_process.return_value = ProcessResult(processed=50)

            job = make_process_job(session_factory, batch_size=50)
            job()

        assert mock_process.call_count == 2

    def test_job_no_procesa_si_no_hay_worker_configurado(self, session_factory):
        with patch("app.scheduler.jobs.process_pending_articles") as mock_process, \
             patch("app.scheduler.jobs.get_model_config", return_value=None):
            job = make_process_job(session_factory)
            job()

        mock_process.assert_not_called()

    def test_job_no_procesa_si_cola_vacia(self, session_factory):
        """Si no hay artículos pendientes, process_pending_articles no se llama."""
        mock_cfg = MagicMock()
        mock_cfg.litellm_model = "groq/llama3-70b-8192"
        mock_cfg.api_key = None
        mock_cfg.base_url = None

        with patch("app.scheduler.jobs.process_pending_articles") as mock_process, \
             patch("app.scheduler.jobs.get_model_config", return_value=mock_cfg), \
             patch("app.scheduler.jobs.count_articles", return_value=0):
            job = make_process_job(session_factory)
            job()

        mock_process.assert_not_called()

    def test_job_hace_commit_por_batch(self, session_factory):
        """El job hace commit después de cada batch."""
        mock_cfg = MagicMock()
        mock_cfg.litellm_model = "groq/llama3-70b-8192"
        mock_cfg.api_key = None
        mock_cfg.base_url = None

        from app.processor.llm import ProcessResult

        with patch("app.scheduler.jobs.process_pending_articles") as mock_process, \
             patch("app.scheduler.jobs.get_model_config", return_value=mock_cfg), \
             patch("app.scheduler.jobs.count_articles", side_effect=[10, 0]):
            mock_process.return_value = ProcessResult(processed=10)

            mock_session = MagicMock()
            mock_session_factory = lambda: mock_session

            job = make_process_job(mock_session_factory)
            job()

        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()


# ─── Tests: make_briefing_job ─────────────────────────────────────────────────

class TestMakeBriefingJob:
    def test_job_genera_briefing(self, session, session_factory, source):
        today = date.today()
        _processed_article(session, source, headline="Hoy en el mundo", score=0.9, day=today)
        session.commit()

        job = make_briefing_job(session_factory)
        job()
        session.commit()

        briefing = get_briefing_by_date(session, today)
        assert briefing is not None
        assert "Hoy en el mundo" in briefing.headlines_text

    def test_job_no_falla_sin_articulos(self, session_factory):
        """El job no explota si no hay artículos para hoy."""
        job = make_briefing_job(session_factory)
        job()  # no debe lanzar excepción


# ─── Tests: create_scheduler ─────────────────────────────────────────────────

class TestCreateScheduler:
    def test_crea_scheduler_con_tres_jobs(self):
        mock_factory = MagicMock()

        scheduler = create_scheduler(
            daily_fetch_time="03:00",
            session_factory=mock_factory,
            sources_config_path="config/sources.yaml",
        )

        job_ids = {job.id for job in scheduler.get_jobs()}
        assert "daily_fetch" in job_ids
        assert "daily_process" in job_ids
        assert "daily_briefing" in job_ids

    def test_jobs_con_horario_correcto(self):
        scheduler = create_scheduler(
            daily_fetch_time="03:00",
            session_factory=MagicMock(),
            sources_config_path="config/sources.yaml",
        )

        jobs = {job.id: job for job in scheduler.get_jobs()}

        # Extraer hora y minuto de cada trigger
        def _trigger_hhmm(job):
            fields = {f.name: f for f in job.trigger.fields}
            return int(str(fields["hour"])), int(str(fields["minute"]))

        fetch_h, fetch_m       = _trigger_hhmm(jobs["daily_fetch"])
        process_h, process_m   = _trigger_hhmm(jobs["daily_process"])
        briefing_h, briefing_m = _trigger_hhmm(jobs["daily_briefing"])

        assert (fetch_h, fetch_m)    == (3, 0)
        assert (process_h, process_m)  == (4, 0)
        assert (briefing_h, briefing_m) == (5, 0)

    def test_horario_wrap_a_medianoche(self):
        """Si DAILY_FETCH_TIME=23:00, el proceso debe quedar en 00:00 del día siguiente."""
        scheduler = create_scheduler(
            daily_fetch_time="23:00",
            session_factory=MagicMock(),
            sources_config_path="config/sources.yaml",
        )
        jobs = {job.id: job for job in scheduler.get_jobs()}

        def _trigger_hhmm(job):
            fields = {f.name: f for f in job.trigger.fields}
            return int(str(fields["hour"])), int(str(fields["minute"]))

        _, _ = _trigger_hhmm(jobs["daily_fetch"])     # 23:00
        ph, pm = _trigger_hhmm(jobs["daily_process"])  # 00:00
        bh, bm = _trigger_hhmm(jobs["daily_briefing"]) # 01:00

        assert (ph, pm) == (0, 0)
        assert (bh, bm) == (1, 0)

    def test_lanza_error_con_tiempo_invalido(self):
        with pytest.raises(ValueError):
            create_scheduler(
                daily_fetch_time="invalid",
                session_factory=MagicMock(),
                sources_config_path="config/sources.yaml",
            )
