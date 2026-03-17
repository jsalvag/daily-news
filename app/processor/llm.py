"""
Procesador de artículos usando LiteLLM (multi-provider).

Soporta cualquier proveedor compatible con LiteLLM: Groq, Anthropic,
OpenAI, Ollama, Mistral, Gemini, etc. El string de modelo sigue el
formato 'provider/model_id' (ej: 'groq/llama3-70b-8192').

Roles de modelo:
  - worker : analiza artículo por artículo (headline + summary + score)
  - editor : síntesis del briefing diario (reservado para uso futuro)

Uso típico:
    from app.processor.llm import process_pending_articles
    result = process_pending_articles(session, model="groq/llama3-70b-8192",
                                      api_key="gsk_...")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import litellm
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Silenciar el spam de debug de LiteLLM
litellm.suppress_debug_info = True

# Máximo de caracteres del contenido del artículo enviados al LLM
MAX_CONTENT_CHARS = 3_000

_SYSTEM_PROMPT = """Eres un editor de noticias experto. Analizas artículos de noticias \
y produces resúmenes concisos en español para un boletín diario de noticias.

Para cada artículo debes:
1. Crear un titular corto y atractivo (máximo 15 palabras) en español
2. Escribir un resumen de 1-2 oraciones en español
3. Asignar una puntuación de relevancia de 0.0 a 1.0 (mayor = más importante)

Responde SOLO en formato JSON válido con la estructura exacta especificada."""


# ─── Modelos de datos ────────────────────────────────────────────────────────

class ArticleAnalysis(BaseModel):
    headline: str = Field(description="Titular corto en español (máx 15 palabras)")
    summary: str = Field(description="Resumen de 1-2 oraciones en español")
    relevance_score: float = Field(ge=0.0, le=1.0, description="Relevancia de 0.0 a 1.0")


@dataclass
class ProcessResult:
    processed: int = 0
    skipped: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (
            f"ProcessResult(processed={self.processed}, "
            f"skipped={self.skipped}, errors={self.errors})"
        )


# ─── Helpers internos ────────────────────────────────────────────────────────

def _build_user_message(article) -> str:
    """Construye el mensaje de usuario con el contenido del artículo."""
    parts = []
    if article.title:
        parts.append(f"Título: {article.title}")
    if article.summary:
        parts.append(f"Extracto: {article.summary}")
    if article.content:
        truncated = article.content[:MAX_CONTENT_CHARS]
        parts.append(f"Contenido: {truncated}")
    return "\n\n".join(parts)


# ─── Funciones públicas ──────────────────────────────────────────────────────

def analyze_article(
    article,
    model: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Optional[ArticleAnalysis]:
    """
    Analiza un artículo individual con LiteLLM.

    Args:
        article:  Objeto Article de la BD.
        model:    String de modelo LiteLLM: 'provider/model_id'.
        api_key:  API key del proveedor (None para Ollama u otros sin auth).
        base_url: URL base para proveedores locales (Ollama, LMStudio).

    Returns:
        ArticleAnalysis si el análisis fue exitoso, None en caso contrario.
    """
    user_content = _build_user_message(article)
    if not user_content.strip():
        logger.debug("Artículo sin contenido, omitiendo: id=%s", article.id)
        return None

    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": ArticleAnalysis,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    try:
        response = litellm.completion(**kwargs)
        raw = response.choices[0].message.content
        if not raw:
            logger.warning("Respuesta vacía del modelo para artículo id=%s", article.id)
            return None
        return ArticleAnalysis.model_validate_json(raw)
    except Exception as exc:
        logger.warning("Error al analizar artículo id=%s con modelo %s: %s", article.id, model, exc)
        return None


def process_pending_articles(
    session,
    model: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    limit: int = 50,
) -> ProcessResult:
    """
    Procesa todos los artículos pendientes con LiteLLM.

    Lee artículos no procesados de la BD, los analiza uno a uno,
    y actualiza sus campos ai_headline, ai_summary y relevance_score.

    Args:
        session:  Sesión de BD activa. El caller hace commit.
        model:    String de modelo LiteLLM: 'provider/model_id'.
        api_key:  API key del proveedor (None para Ollama).
        base_url: URL base para proveedores locales.
        limit:    Máximo de artículos a procesar en esta llamada.

    Returns:
        ProcessResult con contadores de procesados/omitidos/errores.
    """
    from app.storage.crud import get_unprocessed_articles, update_article_ai

    articles = get_unprocessed_articles(session, limit=limit)
    result = ProcessResult()

    for article in articles:
        analysis = analyze_article(article, model=model, api_key=api_key, base_url=base_url)

        if analysis is not None:
            update_article_ai(
                session,
                article.id,
                ai_headline=analysis.headline,
                ai_summary=analysis.summary,
                relevance_score=analysis.relevance_score,
            )
            result.processed += 1
        else:
            # Sin análisis: marcar procesado con fallback para no bloquear la cola
            update_article_ai(
                session,
                article.id,
                ai_headline=article.title or "",
                ai_summary=article.summary or "",
                relevance_score=0.0,
            )
            result.skipped += 1

    logger.info("Procesamiento LLM completado: %s", result)
    return result
