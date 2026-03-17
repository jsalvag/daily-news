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

DEFAULT_SYSTEM_PROMPT = """Eres un editor de noticias experto. Analizas artículos en CUALQUIER idioma y produces titular y resumen SIEMPRE en español, optimizados para ser leídos en voz alta por un sintetizador de voz (TTS).

REGLAS ABSOLUTAS — sin excepciones, sin importar el idioma del artículo original:

1. IDIOMA — SIEMPRE español. NUNCA inglés ni otro idioma en el output.
   Si el artículo está en inglés, francés, portugués u otro idioma, tradúcelo.
   MAL: "Stock market falls sharply amid rate fears"
   BIEN: "La bolsa cae con fuerza ante el temor a nuevas subidas de tasas."

2. NÚMEROS — SIEMPRE en palabras. NUNCA dígitos, ni siquiera en marcadores.
   MAL: 5-2 | $12,100 | 3.5% | 150,000 | 2024
   BIEN: "cinco a dos" | "doce mil cien dólares" | "tres coma cinco por ciento" | "ciento cincuenta mil" | "dos mil veinticuatro"
   Marcadores deportivos: usar "X a Y" → "cinco a dos", "tres a uno".

3. SÍMBOLOS — Expándelos siempre a palabras.
   $ → "dólares" o "pesos" (según contexto)
   % → "por ciento"
   & → "y"
   vs / vs. → "versus" o "contra"
   km → "kilómetros" | km/h → "kilómetros por hora"

4. ABREVIATURAS Y SIGLAS — Expándelas siempre, salvo ONU, FIFA, NASA, OTAN.
   MAL: DT | CEO | IPO | ETF | USD | EE.UU. | USA | PM | AM
   BIEN: "director técnico" | "director ejecutivo" | "oferta pública inicial" | "dólares" | "Estados Unidos"

5. PUNTUACIÓN — Oraciones completas con punto final. Sin guiones, paréntesis ni corchetes.
   MAL: "San Lorenzo 5-2 Defensa y Justicia"
   BIEN: "San Lorenzo derrotó a Defensa y Justicia cinco a dos."

6. TITULAR — Máximo quince palabras. Sujeto + predicado obligatorios.
   Si el titular supera quince palabras, recórtalo conservando lo esencial.

7. RESUMEN — Una o dos oraciones cortas, cada una con punto final.

Antes de escribir el JSON, verificá mentalmente:
- ¿Está todo en español? ✓
- ¿Hay algún dígito? → convertir a palabras
- ¿Hay algún símbolo ($, %, &, -) o sigla no universal? → expandir
- ¿El titular tiene más de quince palabras? → recortar

Responde SOLO en JSON válido con esta estructura exacta:
{"headline": "...", "summary": "...", "relevance_score": 0.0}"""


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
    system_prompt: Optional[str] = None,
    source_instructions: Optional[str] = None,
) -> Optional[ArticleAnalysis]:
    """
    Analiza un artículo individual con LiteLLM.

    Args:
        article:             Objeto Article de la BD.
        model:               String de modelo LiteLLM: 'provider/model_id'.
        api_key:             API key del proveedor (None para Ollama u otros sin auth).
        base_url:            URL base para proveedores locales (Ollama, LMStudio).
        system_prompt:       Prompt del sistema personalizado. Si es None, usa DEFAULT_SYSTEM_PROMPT.
        source_instructions: Instrucciones específicas de la fuente para guiar el análisis.

    Returns:
        ArticleAnalysis si el análisis fue exitoso, None en caso contrario.
    """
    user_content = _build_user_message(article)
    if not user_content.strip():
        logger.debug("Artículo sin contenido, omitiendo: id=%s", article.id)
        return None

    prompt = system_prompt if system_prompt and system_prompt.strip() else DEFAULT_SYSTEM_PROMPT
    if source_instructions and source_instructions.strip():
        prompt = (
            prompt
            + "\n\nINSTRUCCIONES ESPECÍFICAS DE ESTA FUENTE:\n"
            + source_instructions.strip()
        )

    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
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
    system_prompt: Optional[str] = None,
    on_article_done: Optional[object] = None,
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
    from app.storage.crud import get_source_by_id, get_unprocessed_articles, update_article_ai

    articles = get_unprocessed_articles(session, limit=limit)
    result = ProcessResult()

    # Pre-cargar instrucciones por fuente para no repetir queries en el loop
    _source_instructions: dict[int, str | None] = {}
    for _a in articles:
        if _a.source_id not in _source_instructions:
            _src = get_source_by_id(session, _a.source_id)
            _source_instructions[_a.source_id] = _src.instructions if _src else None

    for article in articles:
        analysis = analyze_article(
            article, model=model, api_key=api_key, base_url=base_url,
            system_prompt=system_prompt,
            source_instructions=_source_instructions.get(article.source_id),
        )

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

        if on_article_done is not None:
            try:
                on_article_done(result)
            except Exception:
                pass

    logger.info("Procesamiento LLM completado: %s", result)
    return result
