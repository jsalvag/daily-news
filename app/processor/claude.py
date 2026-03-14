"""
Procesador de artículos usando la API de Claude.

Responsabilidades:
  - Generar un titular breve en español, optimizado para TTS (Google Home)
  - Generar un resumen de 2-3 oraciones en español
  - Asignar una puntuación de relevancia (0.0–1.0)

Diseño:
  - Sync: el scheduler lo corre en un thread pool; FastAPI lo llama directo.
  - Un artículo por request: más robusto frente a errores individuales.
  - Structured output (Pydantic + messages.parse) para garantizar JSON válido.
  - Sin extended thinking: la tarea es clasificación/resumen, no razonamiento
    complejo. Usar thinking aquí sería sobre-ingeniería cara.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import anthropic
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.storage.crud import get_unprocessed_articles, update_article_ai
from app.storage.models import Article

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-6"
# Truncamos el contenido para no desperdiciar tokens en artículos muy largos
MAX_CONTENT_CHARS = 2_000


# ─── Schema de salida estructurada ───────────────────────────────────────────

class ArticleAnalysis(BaseModel):
    """Resultado del análisis de un artículo por Claude."""

    headline: str = Field(
        description=(
            "Titular en español, máximo 15 palabras. "
            "Optimizado para lectura en voz alta por Google Home: "
            "sin abreviaturas, siglas oscuras ni caracteres especiales."
        )
    )
    summary: str = Field(
        description=(
            "Resumen de 2-3 oraciones en español que capture "
            "lo más importante del artículo. Claro y directo."
        )
    )
    relevance_score: float = Field(
        description=(
            "Relevancia del artículo de 0.0 (insignificante) a 1.0 (impacto mayor). "
            "Considera: impacto en la vida cotidiana, novedad, alcance geográfico "
            "y qué tan urgente es saberlo."
        ),
        ge=0.0,
        le=1.0,
    )


# ─── Prompt del sistema ───────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
Eres un editor de noticias experto con audiencia hispanohablante.
Analizas artículos y produces tres cosas:

1. TITULAR (máx. 15 palabras en español)
   - Pensado para ser leído en voz alta por un asistente de voz
   - Sin siglas desconocidas, sin caracteres especiales, sin puntuación innecesaria
   - Activo y directo: "El gobierno anuncia...", no "Se anuncia..."

2. RESUMEN (2-3 oraciones en español)
   - Qué pasó, quiénes están involucrados, qué implica
   - Sin palabras de relleno; cada palabra aporta información

3. PUNTUACIÓN DE RELEVANCIA (0.0 – 1.0)
   - 0.9–1.0: Impacto mayor, afecta a muchas personas, noticia urgente
   - 0.6–0.8: Noticia importante pero no de primera plana
   - 0.3–0.5: Interés moderado, nicho específico
   - 0.0–0.2: Nota menor, muy local o de bajo impacto

Responde SIEMPRE en español, incluso si el artículo original está en otro idioma.
"""


# ─── Análisis de artículo individual ─────────────────────────────────────────

def _build_user_message(article: Article) -> str:
    """Construye el mensaje de usuario con el contenido disponible del artículo."""
    parts: list[str] = [f"Título: {article.title}"]

    if article.summary:
        parts.append(f"Extracto: {article.summary}")

    if article.content:
        truncated = article.content[:MAX_CONTENT_CHARS]
        parts.append(f"Contenido: {truncated}")

    if article.url:
        parts.append(f"URL: {article.url}")

    return "\n\n".join(parts)


def analyze_article(
    article: Article,
    client: anthropic.Anthropic,
) -> ArticleAnalysis | None:
    """
    Analiza un artículo con Claude y retorna el análisis estructurado.

    Returns:
        ArticleAnalysis si el análisis fue exitoso.
        None si el artículo no tiene suficiente contenido o hubo un error
        irrecuperable (después de los reintentos automáticos del SDK).
    """
    # El SDK reintenta 429 y 5xx automáticamente (max_retries=2 por defecto)
    if not article.title and not article.summary:
        logger.warning("Artículo %d sin contenido: omitido.", article.id)
        return None

    user_message = _build_user_message(article)

    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            output_format=ArticleAnalysis,
        )
    except anthropic.BadRequestError as exc:
        logger.error("BadRequest al analizar artículo %d: %s", article.id, exc)
        return None
    except anthropic.APIError as exc:
        logger.error("APIError al analizar artículo %d: %s", article.id, exc)
        return None

    if response.parsed_output is None:
        # Claude respondió pero el output no coincide con el schema
        logger.warning(
            "Artículo %d: parsed_output es None (stop_reason=%s).",
            article.id,
            response.stop_reason,
        )
        return None

    return response.parsed_output


# ─── Procesamiento por lotes ──────────────────────────────────────────────────

@dataclass
class ProcessResult:
    """Resumen de una corrida de procesamiento IA."""
    processed: int = 0
    skipped: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (
            f"IA: {self.processed} procesados, "
            f"{self.skipped} omitidos, "
            f"{self.errors} errores"
        )


def process_pending_articles(
    session: Session,
    client: anthropic.Anthropic,
    limit: int = 50,
) -> ProcessResult:
    """
    Procesa artículos pendientes de análisis IA.

    Fetcha hasta `limit` artículos sin procesar, los analiza con Claude
    y guarda los resultados. El caller hace session.commit().

    Los artículos que fallan o no tienen contenido se marcan igualmente
    como procesados (con valores por defecto) para no quedar atascados
    en la cola indefinidamente.

    Args:
        session: Sesión SQLAlchemy activa.
        client:  Cliente Anthropic inicializado.
        limit:   Máximo de artículos a procesar en esta corrida.

    Returns:
        ProcessResult con contadores de la operación.
    """
    articles = get_unprocessed_articles(session, limit=limit)
    result = ProcessResult()

    for article in articles:
        analysis = analyze_article(article, client)

        if analysis is None:
            # Sin análisis → guardamos el título original como fallback
            # y relevancia baja para que no bloquee el briefing
            update_article_ai(
                session,
                article.id,
                ai_headline=article.title or "(sin título)",
                ai_summary=article.summary or "",
                relevance_score=0.2,
            )
            result.skipped += 1
            logger.debug("Artículo %d omitido (sin análisis).", article.id)
            continue

        update_article_ai(
            session,
            article.id,
            ai_headline=analysis.headline,
            ai_summary=analysis.summary,
            relevance_score=analysis.relevance_score,
        )
        result.processed += 1
        logger.info(
            "Artículo %d procesado — score=%.2f — %r",
            article.id,
            analysis.relevance_score,
            analysis.headline,
        )

    logger.info(str(result))
    return result
