"""
Tests para el procesador de artículos con LiteLLM (multi-provider).

Estrategia de mocking: se mockea `litellm.completion` por completo — ningún
test hace llamadas reales a la API. Se construye una respuesta fake que imita
la estructura `ModelResponse` de LiteLLM.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.processor.llm import (
    ArticleAnalysis,
    ProcessResult,
    analyze_article,
    process_pending_articles,
)
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


def _mock_litellm_response(analysis: ArticleAnalysis | None) -> MagicMock:
    """
    Construye un objeto de respuesta que imita litellm.ModelResponse.
    response.choices[0].message.content → JSON string de ArticleAnalysis.
    """
    content = analysis.model_dump_json() if analysis is not None else ""
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


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

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(expected)
            result = analyze_article(article, model="groq/llama3-70b-8192")

        assert result is not None
        assert result.headline == "Titular de prueba en español"
        assert result.relevance_score == 0.75

    def test_llama_api_con_modelo_correcto(self, session, source):
        article = _make_article(session, source)

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(_sample_analysis())
            analyze_article(article, model="groq/llama3-70b-8192")

        call_kwargs = mock_completion.call_args.kwargs
        assert call_kwargs["model"] == "groq/llama3-70b-8192"

    def test_pasa_api_key_si_se_provee(self, session, source):
        article = _make_article(session, source)

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(_sample_analysis())
            analyze_article(article, model="groq/llama3-70b-8192", api_key="gsk_test123")

        call_kwargs = mock_completion.call_args.kwargs
        assert call_kwargs["api_key"] == "gsk_test123"

    def test_pasa_base_url_si_se_provee(self, session, source):
        article = _make_article(session, source)

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(_sample_analysis())
            analyze_article(article, model="ollama/llama3", base_url="http://localhost:11434")

        call_kwargs = mock_completion.call_args.kwargs
        assert call_kwargs["base_url"] == "http://localhost:11434"

    def test_no_pasa_api_key_si_es_none(self, session, source):
        article = _make_article(session, source)

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(_sample_analysis())
            analyze_article(article, model="ollama/llama3")

        call_kwargs = mock_completion.call_args.kwargs
        assert "api_key" not in call_kwargs

    def test_incluye_titulo_en_mensaje(self, session, source):
        article = _make_article(session, source, title="Mi título", summary="Mi extracto.")

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(_sample_analysis())
            analyze_article(article, model="groq/llama3-70b-8192")

        call_kwargs = mock_completion.call_args.kwargs
        messages = call_kwargs["messages"]
        user_content = messages[1]["content"]   # messages[0] = system, messages[1] = user
        assert "Mi título" in user_content
        assert "Mi extracto." in user_content

    def test_trunca_contenido_largo(self, session, source):
        long_content = "X" * 5_000
        article = _make_article(session, source, content=long_content)

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(_sample_analysis())
            analyze_article(article, model="groq/llama3-70b-8192")

        call_kwargs = mock_completion.call_args.kwargs
        user_content = call_kwargs["messages"][1]["content"]
        # El contenido truncado no puede superar MAX_CONTENT_CHARS
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

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            result = analyze_article(article, model="groq/llama3-70b-8192")

        assert result is None
        mock_completion.assert_not_called()

    def test_retorna_none_en_excepcion_litellm(self, session, source):
        article = _make_article(session, source)

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.side_effect = Exception("Connection refused")
            result = analyze_article(article, model="groq/llama3-70b-8192")

        assert result is None

    def test_retorna_none_si_contenido_respuesta_vacio(self, session, source):
        article = _make_article(session, source)

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(None)
            result = analyze_article(article, model="groq/llama3-70b-8192")

        assert result is None

    def test_usa_response_format_articlearticle_analysis(self, session, source):
        article = _make_article(session, source)

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(_sample_analysis())
            analyze_article(article, model="groq/llama3-70b-8192")

        call_kwargs = mock_completion.call_args.kwargs
        assert call_kwargs["response_format"] is ArticleAnalysis


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

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(_sample_analysis(relevance_score=0.8))
            result = process_pending_articles(session, model="groq/llama3-70b-8192", limit=10)
            session.commit()

        assert result.processed == 3
        assert result.skipped == 0
        assert result.errors == 0

    def test_marca_articulos_como_procesados_en_bd(self, session, source):
        article = _make_article(session, source)

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(
                _sample_analysis(headline="Titular IA", relevance_score=0.9)
            )
            process_pending_articles(session, model="groq/llama3-70b-8192", limit=10)
            session.commit()
            session.refresh(article)

        assert article.processed is True
        assert article.ai_headline == "Titular IA"
        assert abs(article.relevance_score - 0.9) < 1e-6

    def test_articulo_sin_contenido_se_omite_y_marca_procesado(self, session, source):
        """Artículo sin título ni summary no llama al LLM pero queda marcado procesado."""
        article = Article(
            source_id=source.id,
            title="",
            url="https://test.com/empty",
            guid="guid-empty-2",
            summary=None,
        )
        session.add(article)
        session.commit()

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            result = process_pending_articles(session, model="groq/llama3-70b-8192", limit=10)
            session.commit()
            session.refresh(article)

        assert result.skipped == 1
        assert result.processed == 0
        assert article.processed is True   # no queda atascado en la cola
        mock_completion.assert_not_called()

    def test_respeta_limit(self, session, source):
        for i in range(5):
            session.add(Article(
                source_id=source.id,
                title=f"Noticia {i}",
                url=f"https://test.com/{i}",
                guid=f"guid-lim-{i}",
            ))
        session.commit()

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(_sample_analysis())
            result = process_pending_articles(session, model="groq/llama3-70b-8192", limit=2)

        assert mock_completion.call_count == 2
        assert result.processed == 2

    def test_fallo_api_cuenta_como_skipped(self, session, source):
        """Si el LLM falla, el artículo se omite con valores por defecto."""
        article = _make_article(session, source, title="Título original")

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.side_effect = Exception("API Error")
            result = process_pending_articles(session, model="groq/llama3-70b-8192", limit=10)
            session.commit()
            session.refresh(article)

        assert result.skipped == 1
        assert article.processed is True         # no queda atascado
        assert article.ai_headline == "Título original"  # fallback al título original

    def test_lista_vacia_no_llama_al_llm(self, session, source):
        with patch("app.processor.llm.litellm.completion") as mock_completion:
            result = process_pending_articles(session, model="groq/llama3-70b-8192", limit=10)

        mock_completion.assert_not_called()
        assert result.processed == 0

    def test_pasa_api_key_y_base_url_al_llm(self, session, source):
        _make_article(session, source)

        with patch("app.processor.llm.litellm.completion") as mock_completion:
            mock_completion.return_value = _mock_litellm_response(_sample_analysis())
            process_pending_articles(
                session,
                model="ollama/llama3",
                api_key=None,
                base_url="http://localhost:11434",
                limit=10,
            )

        call_kwargs = mock_completion.call_args.kwargs
        assert call_kwargs["base_url"] == "http://localhost:11434"
        assert "api_key" not in call_kwargs


# ─── Tests: ProcessResult ─────────────────────────────────────────────────────

class TestProcessResult:
    def test_str_incluye_todos_los_contadores(self):
        r = ProcessResult(processed=5, skipped=2, errors=1)
        s = str(r)
        assert "5" in s
        assert "2" in s
        assert "1" in s

    def test_valores_por_defecto(self):
        r = ProcessResult()
        assert r.processed == 0
        assert r.skipped == 0
        assert r.errors == 0
