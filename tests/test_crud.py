"""
Tests para las operaciones CRUD de la base de datos.
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.fetcher.rss import FetchedArticle
from app.storage.crud import (
    article_exists,
    delete_source,
    get_all_sources,
    get_articles_for_date,
    get_briefing_by_date,
    get_latest_briefing,
    get_source_by_id,
    get_source_by_url,
    get_sources_by_category,
    get_unprocessed_articles,
    mark_source_fetched,
    save_articles,
    toggle_source,
    update_article_ai,
    upsert_briefing,
)
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
def source(session) -> Source:
    """Fuente habilitada de prueba, ya persistida."""
    src = Source(
        name="Infobae",
        url="https://www.infobae.com/feeds/rss/",
        category="argentina",
        source_type="rss",
        language="es",
        enabled=True,
    )
    session.add(src)
    session.commit()
    return src


@pytest.fixture
def disabled_source(session) -> Source:
    """Fuente deshabilitada de prueba."""
    src = Source(
        name="La Nación",
        url="https://www.lanacion.com.ar/rss/",
        category="argentina",
        source_type="rss",
        language="es",
        enabled=False,
    )
    session.add(src)
    session.commit()
    return src


def _make_fetched(
    title: str = "Título de prueba",
    url: str = "https://example.com/nota",
    guid: str = "guid-1",
    published_at: datetime | None = None,
) -> FetchedArticle:
    return FetchedArticle(
        title=title,
        url=url,
        guid=guid,
        source_url="https://example.com/rss",
        summary="Resumen de prueba.",
        content=None,
        published_at=published_at or datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


# ─── Tests: Sources ───────────────────────────────────────────────────────────

class TestGetAllSources:
    def test_retorna_todas_las_fuentes(self, session, source, disabled_source):
        result = get_all_sources(session)
        assert len(result) == 2

    def test_filtro_enabled_only(self, session, source, disabled_source):
        result = get_all_sources(session, enabled_only=True)
        assert len(result) == 1
        assert result[0].name == "Infobae"

    def test_retorna_lista_vacia_si_no_hay_fuentes(self, session):
        assert get_all_sources(session) == []


class TestGetSourcesByCategory:
    def test_retorna_fuentes_de_categoria(self, session, source):
        result = get_sources_by_category(session, "argentina")
        assert len(result) == 1
        assert result[0].url == source.url

    def test_no_retorna_fuentes_deshabilitadas(self, session, source, disabled_source):
        # Ambas son de "argentina", pero solo source está habilitada
        result = get_sources_by_category(session, "argentina")
        urls = [s.url for s in result]
        assert disabled_source.url not in urls

    def test_categoria_inexistente_retorna_vacio(self, session, source):
        result = get_sources_by_category(session, "inexistente")
        assert result == []


class TestGetSourceByUrl:
    def test_encuentra_fuente_existente(self, session, source):
        found = get_source_by_url(session, source.url)
        assert found is not None
        assert found.name == "Infobae"

    def test_retorna_none_si_no_existe(self, session):
        assert get_source_by_url(session, "https://noexiste.com/rss") is None


class TestGetSourceById:
    def test_encuentra_fuente_por_id(self, session, source):
        found = get_source_by_id(session, source.id)
        assert found is not None
        assert found.url == source.url

    def test_retorna_none_para_id_inexistente(self, session):
        assert get_source_by_id(session, 9999) is None


class TestToggleSource:
    def test_deshabilita_fuente(self, session, source):
        result = toggle_source(session, source.id, enabled=False)
        session.commit()
        assert result is True
        session.refresh(source)
        assert source.enabled is False

    def test_habilita_fuente_deshabilitada(self, session, disabled_source):
        result = toggle_source(session, disabled_source.id, enabled=True)
        session.commit()
        assert result is True
        session.refresh(disabled_source)
        assert disabled_source.enabled is True

    def test_retorna_false_para_id_inexistente(self, session):
        assert toggle_source(session, 9999, enabled=False) is False


class TestDeleteSource:
    def test_elimina_fuente_existente(self, session, source):
        source_id = source.id
        result = delete_source(session, source_id)
        session.commit()
        assert result is True
        assert get_source_by_id(session, source_id) is None

    def test_retorna_false_para_id_inexistente(self, session):
        assert delete_source(session, 9999) is False

    def test_elimina_articulos_en_cascada(self, session, source):
        """Al borrar una fuente, sus artículos también se eliminan."""
        article = Article(
            source_id=source.id,
            title="Nota",
            url="https://example.com/nota",
            guid="guid-cascade",
        )
        session.add(article)
        session.commit()
        article_id = article.id

        delete_source(session, source.id)
        session.commit()

        result = session.get(Article, article_id)
        assert result is None


class TestMarkSourceFetched:
    def test_actualiza_last_fetched_at(self, session, source):
        assert source.last_fetched_at is None
        mark_source_fetched(session, source.id)
        session.commit()
        session.refresh(source)
        assert source.last_fetched_at is not None


# ─── Tests: Articles ──────────────────────────────────────────────────────────

class TestArticleExists:
    def test_retorna_false_si_no_existe(self, session, source):
        assert article_exists(session, "guid-xyz", source.id) is False

    def test_retorna_true_si_existe(self, session, source):
        article = Article(
            source_id=source.id,
            title="Nota",
            url="https://example.com/nota",
            guid="guid-abc",
        )
        session.add(article)
        session.commit()
        assert article_exists(session, "guid-abc", source.id) is True

    def test_guid_de_otra_fuente_no_cuenta(self, session, source, disabled_source):
        article = Article(
            source_id=source.id,
            title="Nota",
            url="https://example.com/nota",
            guid="guid-shared",
        )
        session.add(article)
        session.commit()
        # El mismo guid pero de otra fuente → no existe para disabled_source
        assert article_exists(session, "guid-shared", disabled_source.id) is False


class TestSaveArticles:
    def test_inserta_articulos_nuevos(self, session, source):
        fetched = [_make_fetched(guid="g1"), _make_fetched(guid="g2", url="https://example.com/2")]
        count = save_articles(session, fetched, source.id)
        session.commit()
        assert count == 2

    def test_no_duplica_articulos_existentes(self, session, source):
        fetched = [_make_fetched(guid="g1")]
        save_articles(session, fetched, source.id)
        session.commit()

        # Segunda llamada con el mismo guid
        count = save_articles(session, fetched, source.id)
        session.commit()
        assert count == 0

    def test_deduplicacion_dentro_del_mismo_batch(self, session, source):
        """Dos entradas con el mismo guid en la misma lista solo se insertan una vez."""
        fetched = [_make_fetched(guid="dup"), _make_fetched(guid="dup", url="https://example.com/2")]
        count = save_articles(session, fetched, source.id)
        session.commit()
        assert count == 1

    def test_retorna_cero_con_lista_vacia(self, session, source):
        assert save_articles(session, [], source.id) == 0


class TestGetArticlesForDate:
    def _article(self, session, source, guid: str, published_at: datetime) -> Article:
        a = Article(
            source_id=source.id,
            title=f"Nota {guid}",
            url=f"https://example.com/{guid}",
            guid=guid,
            published_at=published_at,
        )
        session.add(a)
        return a

    def test_retorna_articulos_del_dia(self, session, source):
        self._article(session, source, "a1", datetime(2024, 1, 15, 8, 0))
        self._article(session, source, "a2", datetime(2024, 1, 15, 20, 0))
        self._article(session, source, "a3", datetime(2024, 1, 16, 8, 0))  # otro día
        session.commit()

        result = get_articles_for_date(session, date(2024, 1, 15))
        guids = {a.guid for a in result}
        assert guids == {"a1", "a2"}

    def test_filtro_processed_only(self, session, source):
        a = self._article(session, source, "p1", datetime(2024, 1, 15, 8, 0))
        b = self._article(session, source, "p2", datetime(2024, 1, 15, 9, 0))
        session.commit()
        session.refresh(a)
        session.refresh(b)

        # Procesamos solo uno
        update_article_ai(session, a.id, "Titular IA", "Resumen IA", 0.9)
        session.commit()

        result = get_articles_for_date(session, date(2024, 1, 15), processed_only=True)
        assert len(result) == 1
        assert result[0].guid == "p1"

    def test_dia_sin_articulos_retorna_vacio(self, session, source):
        result = get_articles_for_date(session, date(2024, 1, 15))
        assert result == []


class TestGetUnprocessedArticles:
    def test_retorna_solo_no_procesados(self, session, source):
        for i in range(3):
            session.add(Article(
                source_id=source.id,
                title=f"Nota {i}",
                url=f"https://example.com/{i}",
                guid=f"g{i}",
                published_at=datetime(2024, 1, 15, i, 0),
            ))
        session.commit()

        # Procesamos uno
        all_arts = get_unprocessed_articles(session, limit=10)
        update_article_ai(session, all_arts[0].id, "H", "S", 0.5)
        session.commit()

        result = get_unprocessed_articles(session, limit=10)
        assert len(result) == 2

    def test_respeta_limit(self, session, source):
        for i in range(5):
            session.add(Article(
                source_id=source.id,
                title=f"Nota {i}",
                url=f"https://example.com/{i}",
                guid=f"g{i}",
            ))
        session.commit()

        result = get_unprocessed_articles(session, limit=3)
        assert len(result) == 3


class TestUpdateArticleAi:
    def test_guarda_datos_ia_y_marca_procesado(self, session, source):
        article = Article(
            source_id=source.id,
            title="Nota original",
            url="https://example.com/nota",
            guid="g-ai",
        )
        session.add(article)
        session.commit()
        session.refresh(article)

        update_article_ai(session, article.id, "Titular IA", "Resumen IA", 0.87)
        session.commit()
        session.refresh(article)

        assert article.ai_headline == "Titular IA"
        assert article.ai_summary == "Resumen IA"
        assert abs(article.relevance_score - 0.87) < 1e-6
        assert article.processed is True


# ─── Tests: DailyBriefing ─────────────────────────────────────────────────────

class TestGetBriefingByDate:
    def test_retorna_none_si_no_existe(self, session):
        result = get_briefing_by_date(session, date(2024, 1, 15))
        assert result is None

    def test_retorna_briefing_existente(self, session):
        session.add(DailyBriefing(
            date="2024-01-15",
            headlines_text="Titular 1",
            full_text="Texto completo",
            article_ids="1,2,3",
        ))
        session.commit()

        result = get_briefing_by_date(session, date(2024, 1, 15))
        assert result is not None
        assert result.date == "2024-01-15"


class TestUpsertBriefing:
    def test_crea_nuevo_briefing(self, session):
        result = upsert_briefing(
            session,
            date(2024, 1, 15),
            "Titulares del día",
            "Texto completo del briefing",
            [1, 2, 3],
        )
        session.commit()

        assert result.date == "2024-01-15"
        assert result.headlines_text == "Titulares del día"
        assert result.article_ids == "1,2,3"

    def test_actualiza_briefing_existente(self, session):
        upsert_briefing(session, date(2024, 1, 15), "Titulares v1", "Texto v1", [1])
        session.commit()

        updated = upsert_briefing(session, date(2024, 1, 15), "Titulares v2", "Texto v2", [1, 2])
        session.commit()

        assert updated.headlines_text == "Titulares v2"
        assert updated.article_ids == "1,2"

        # Solo debe existir un briefing para ese día
        stmt = select(DailyBriefing).where(DailyBriefing.date == "2024-01-15")
        all_briefings = session.execute(stmt).scalars().all()
        assert len(all_briefings) == 1


class TestGetLatestBriefing:
    def test_retorna_none_si_no_hay_briefings(self, session):
        assert get_latest_briefing(session) is None

    def test_retorna_el_mas_reciente(self, session):
        session.add(DailyBriefing(date="2024-01-13", headlines_text="Viejo", full_text="", article_ids=""))
        session.add(DailyBriefing(date="2024-01-15", headlines_text="Nuevo", full_text="", article_ids=""))
        session.add(DailyBriefing(date="2024-01-14", headlines_text="Medio", full_text="", article_ids=""))
        session.commit()

        result = get_latest_briefing(session)
        assert result is not None
        assert result.date == "2024-01-15"
