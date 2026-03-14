"""
Tests para el procesador de artículos con Claude.

Estrategia de mocking: se mockea anthropic.Anthropic por completo — ningún
test hace llamadas reales a la API. Se usa unittest.mock.MagicMock para
simular las respuestas del SDK.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import anthropic
import pytest

from app.processor.claude import (
    ArticleAnalysis,
    ProcessResult,
    analyze_article,
    process_pending_articles,
)
from app.storage.crud import get_unprocessed_articles
from app.storage.models import Article, Source, create_db_engine, get_session_factory, init_db


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
    src = Source(
        name="Test Source",
        url="https://test.com/rss",
        category="tecnologia",
        language="en",
        enabled=True,
    )
    session.add(src)
    session.commit()
    return src


def _make_article(session, source, *, title="Test Article", summary="A test summary.", content=None) -> Article:
    a = Article(
        source_id=source.id,
        title=title,
        url="https://test.com/article-1",
        guid="guid-test-1",
        summary=summary,
        content=content,
    )
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


def _mock_client(analysis: ArticleAnalysis | None = None, stop_reason: str = "end_turn") -> MagicMock:
    """Construye un cliente Anthropic mock que retorna el análisis dado."""
    mock_response = MagicMock()
    mock_response.parsed_output = analysis
    mock_response.stop_reason = stop_reason

    # No usamos spec= porque en anthropic>=0.40 'messages' es un atributo de
    # instancia (creado en __init__), no de clase, y spec=Anthropic lo bloquea.
    client = MagicMock()
    client.messages.parse.return_value = mock_response
    return client


def _sample_analysis(**kwargs) -> ArticleAnalysis:
    defaults = dict(
        headline="Titular de prueba en español",
        summary="Resumen breve del artículo de prueba.",
        relevance_score=0.75,
    )
    defaults.update(kwargs)
    return ArticleAnalysis(**defaults)


# ─── Tests: analyze_article ───────────────────────────────────────────────────

class TestAnalyzeArticle:
    def test_retorna_analysis_exitoso(self, session, source):
        article = _make_article(session, source)
        expected = _sample_analysis()
        client = _mock_client(expected)

        result = analyze_article(article, client)

        assert result is not None
        assert result.headline == "Titular de prueba en español"
        assert result.relevance_score == 0.75

    def test_llama_api_con_contenido_del_articulo(self, session, source):
        article = _make_article(session, source, title="Mi título", summary="Mi extracto.")
        client = _mock_client(_sample_analysis())

        analyze_article(article, client)

        call_args = client.messages.parse.call_args
        messages = call_args.kwargs["messages"]
        user_content = messages[0]["content"]
        assert "Mi título" in user_content
        assert "Mi extracto." in user_content

    def test_trunca_contenido_largo(self, session, source):
        long_content = "X" * 5_000
        article = _make_article(session, source, content=long_content)
        client = _mock_client(_sample_analysis())

        analyze_article(article, client)

        call_args = client.messages.parse.call_args
        user_content = call_args.kwargs["messages"][0]["content"]
        # El contenido en el mensaje no puede superar MAX_CONTENT_CHARS
        assert len(user_content) < 5_000

    def test_retorna_none_si_sin_titulo_y_sin_summary(self, session, source):
        article = Article(
            source_id=source.id,
            title="",
            url="https://test.com/empty",
            guid="guid-empty",
            summary=None,
        )
        session.add(article)
        session.commit()
        client = MagicMock()

        result = analyze_article(article, client)

        assert result is None
        client.messages.parse.assert_not_called()

    def test_retorna_none_en_bad_request(self, session, source):
        article = _make_article(session, source)
        client = MagicMock()
        # APIStatusError requiere response con .request y .status_code
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.request = MagicMock()
        client.messages.parse.side_effect = anthropic.BadRequestError(
            message="invalid input",
            response=mock_response,
            body={},
        )

        result = analyze_article(article, client)

        assert result is None

    def test_retorna_none_en_api_error_generico(self, session, source):
        article = _make_article(session, source)
        client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.request = MagicMock()
        client.messages.parse.side_effect = anthropic.InternalServerError(
            message="server error",
            response=mock_response,
            body={},
        )

        result = analyze_article(article, client)

        assert result is None

    def test_retorna_none_si_parsed_output_es_none(self, session, source):
        """Claude respondió pero el output no coincide con el schema."""
        article = _make_article(session, source)
        client = _mock_client(analysis=None, stop_reason="refusal")

        result = analyze_article(article, client)

        assert result is None

    def test_usa_modelo_correcto(self, session, source):
        article = _make_article(session, source)
        client = _mock_client(_sample_analysis())

        analyze_article(article, client)

        call_kwargs = client.messages.parse.call_args.kwargs
        assert call_kwargs["model"] == "claude-opus-4-6"

    def test_usa_output_format_correcto(self, session, source):
        article = _make_article(session, source)
        client = _mock_client(_sample_analysis())

        analyze_article(article, client)

        call_kwargs = client.messages.parse.call_args.kwargs
        assert call_kwargs["output_format"] is ArticleAnalysis


# ─── Tests: process_pending_articles ─────────────────────────────────────────

class TestProcessPendingArticles:
    def test_procesa_articulos_pendientes(self, session, source):
        for i in range(3):
            session.add(Article(
                source_id=source.id,
                title=f"Noticia {i}",
                url=f"https://test.com/{i}",
                guid=f"guid-{i}",
                summary=f"Resumen {i}.",
            ))
        session.commit()

        client = _mock_client(_sample_analysis(relevance_score=0.8))
        result = process_pending_articles(session, client, limit=10)
        session.commit()

        assert result.processed == 3
        assert result.skipped == 0
        assert result.errors == 0

    def test_marca_articulos_como_procesados_en_bd(self, session, source):
        article = _make_article(session, source)
        client = _mock_client(_sample_analysis(headline="Titular IA", relevance_score=0.9))

        process_pending_articles(session, client, limit=10)
        session.commit()
        session.refresh(article)

        assert article.processed is True
        assert article.ai_headline == "Titular IA"
        assert abs(article.relevance_score - 0.9) < 1e-6

    def test_articulo_sin_contenido_se_omite_y_marca_procesado(self, session, source):
        """Artículo sin título ni summary no llama a Claude pero queda marcado."""
        article = Article(
            source_id=source.id,
            title="",
            url="https://test.com/empty",
            guid="guid-empty-2",
            summary=None,
        )
        session.add(article)
        session.commit()

        client = MagicMock()
        result = process_pending_articles(session, client, limit=10)
        session.commit()
        session.refresh(article)

        assert result.skipped == 1
        assert result.processed == 0
        assert article.processed is True         # no queda atascado en la cola
        client.messages.parse.assert_not_called()

    def test_respeta_limit(self, session, source):
        for i in range(5):
            session.add(Article(
                source_id=source.id,
                title=f"Noticia {i}",
                url=f"https://test.com/{i}",
                guid=f"guid-lim-{i}",
            ))
        session.commit()

        client = _mock_client(_sample_analysis())
        result = process_pending_articles(session, client, limit=2)

        assert client.messages.parse.call_count == 2
        assert result.processed == 2

    def test_fallo_de_api_cuenta_como_skipped(self, session, source):
        """Si la API falla, el artículo se omite con valores por defecto."""
        article = _make_article(session, source)
        client = MagicMock()
        mock_response = MagicMock()
        mock_response.parsed_output = None
        mock_response.stop_reason = "refusal"
        client.messages.parse.return_value = mock_response

        result = process_pending_articles(session, client, limit=10)
        session.commit()
        session.refresh(article)

        assert result.skipped == 1
        assert article.processed is True   # no queda atascado
        assert article.ai_headline == article.title   # fallback al título original

    def test_lista_vacia_no_llama_a_claude(self, session, source):
        client = MagicMock()
        result = process_pending_articles(session, client, limit=10)

        client.messages.parse.assert_not_called()
        assert result.processed == 0


# ─── Tests: ProcessResult ─────────────────────────────────────────────────────

class TestProcessResult:
    def test_str_incluye_todos_los_contadores(self):
        r = ProcessResult(processed=5, skipped=2, errors=1)
        s = str(r)
        assert "5" in s
        assert "2" in s
        assert "1" in s
