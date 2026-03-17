"""
Jobs del scheduler y lógica de generación del briefing diario.

Estructura:
  - generate_daily_briefing(): crea/actualiza el briefing de un día dado.
    Es una función pura (no toca el scheduler) — se puede llamar desde
    la API también para regenerar un briefing manualmente.

  - make_*_job(): factories que retornan callables para APScheduler.
    Cada job crea su propia sesión (las sesiones SQLAlchemy no son
    thread-safe; APScheduler corre jobs en threads separados).

  - create_scheduler(): ensambla el BackgroundScheduler con los tres
    jobs diarios y lo retorna listo para ser iniciado.

Horario por defecto (configurable via DAILY_FETCH_TIME en .env o BD):
  - fetch_time         → fetch de todos los feeds
  - fetch_time + 1h   → procesamiento IA
  - fetch_time + 2h   → generación del briefing

Así, si DAILY_FETCH_TIME=03:00, el briefing queda listo a las 05:00,
bien antes del momento en que el usuario se levanta y pregunta al Google Home.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.events import publish
from app.fetcher.rss import fetch_feeds_concurrently
from app.fetcher.sources_loader import load_and_sync
from app.processor.llm import process_pending_articles
from app.storage.crud import (
    count_articles,
    get_all_sources,
    get_articles_for_date,
    get_model_config,
    get_tts_config,
    get_worker_system_prompt,
    mark_source_fetched,
    save_articles,
    update_briefing_audio,
    upsert_briefing,
)
from app.storage.models import DailyBriefing

logger = logging.getLogger(__name__)

# Artículos máximos en cada sección del briefing
_MAX_VOICE_ARTICLES = 7    # titulares para Google Home (~90 segundos hablados)
_MAX_FULL_ARTICLES  = 15   # artículos con resumen para la lectura en iPhone


# ─── Generación del briefing diario ──────────────────────────────────────────

def generate_daily_briefing(
    session: Session,
    target_date: date | None = None,
    run_at: str | None = None,
) -> DailyBriefing | None:
    """
    Genera o regenera el briefing diario a partir de los artículos procesados.

    Toma los artículos del día ordenados por relevancia, construye:
      - `headlines_text`: titulares numerados para TTS (Google Home)
      - `full_text`: titular + resumen por artículo para lectura en iPhone

    Args:
        session:     Sesión activa. El caller hace commit.
        target_date: Fecha del briefing. Por defecto, hoy.
        run_at:      Timestamp YYYY-MM-DD HH:MM de esta ejecución. Por defecto, ahora.

    Returns:
        El DailyBriefing creado/actualizado, o None si no hay artículos.
    """
    if target_date is None:
        target_date = date.today()
    if run_at is None:
        run_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    articles = get_articles_for_date(session, target_date, processed_only=True)

    if not articles:
        logger.warning("Sin artículos procesados para %s — briefing no generado.", target_date)
        return None

    # ── Sección de voz (Google Home) ────────────────────────────────────────
    voice_articles = articles[:_MAX_VOICE_ARTICLES]
    headlines_lines = [
        # Punto final en cada ítem → el TTS hace pausa natural entre oraciones
        f"{i + 1}. {a.ai_headline.rstrip('.')}."
        for i, a in enumerate(voice_articles)
    ]
    headlines_text = " ".join(headlines_lines)

    # ── Sección completa (iPhone) ────────────────────────────────────────────
    full_lines: list[str] = []
    for a in articles[:_MAX_FULL_ARTICLES]:
        full_lines.append(f"• {a.ai_headline}")
        if a.ai_summary:
            full_lines.append(f"  {a.ai_summary}")
        full_lines.append("")  # línea en blanco entre artículos

    full_text = "\n".join(full_lines).strip()
    article_ids = [a.id for a in articles[:_MAX_FULL_ARTICLES]]

    briefing = upsert_briefing(session, target_date, headlines_text, full_text, article_ids, run_at=run_at)
    logger.info(
        "Briefing %s (run_at=%s) generado: %d titulares voz, %d artículos completos.",
        target_date,
        run_at,
        len(voice_articles),
        len(article_ids),
    )
    return briefing


# ─── Job factories ────────────────────────────────────────────────────────────

def make_fetch_job(
    session_factory: Callable[[], Session],
    sources_config_path: str,
) -> Callable[[], None]:
    """
    Retorna el job de fetch: sincroniza sources.yaml y descarga todos los feeds.
    """
    import asyncio

    def _job() -> None:
        logger.info("Job fetch: iniciando.")
        publish("job_start", {"job": "fetch"})
        session = session_factory()
        try:
            # 1. Sincronizar fuentes del YAML con la BD (por si el usuario editó el YAML)
            try:
                load_and_sync(sources_config_path, session)
                session.commit()
            except Exception as exc:
                logger.error("Error al sincronizar sources.yaml: %s", exc)

            # 2. Obtener URLs de todas las fuentes habilitadas
            sources = get_all_sources(session, enabled_only=True)
            if not sources:
                logger.warning("No hay fuentes habilitadas — fetch cancelado.")
                publish("job_done", {"job": "fetch", "inserted": 0})
                return

            urls = [s.url for s in sources]
            url_to_source = {s.url: s for s in sources}

            # 3. Fetch concurrente
            results = asyncio.run(fetch_feeds_concurrently(urls))

            # 4. Persistir artículos nuevos
            total_inserted = 0
            for url, fetched_articles in results.items():
                src = url_to_source.get(url)
                if src is None:
                    continue
                inserted = save_articles(session, fetched_articles, src.id)
                if inserted > 0:
                    mark_source_fetched(session, src.id)
                total_inserted += inserted
                publish("fetch_progress", {
                    "name": getattr(src, "name", None) or url,
                    "url": url,
                    "count": len(fetched_articles),
                    "inserted": inserted,
                })

            session.commit()
            logger.info("Job fetch: %d artículos nuevos insertados.", total_inserted)
            publish("job_done", {"job": "fetch", "inserted": total_inserted})

        except Exception as exc:
            logger.exception("Job fetch: error inesperado: %s", exc)
            session.rollback()
            publish("job_error", {"job": "fetch", "error": str(exc)})
        finally:
            session.close()

    return _job


def make_process_job(
    session_factory: Callable[[], Session],
    batch_size: int = 50,
    batch_delay_seconds: float = 30.0,
) -> Callable[[], None]:
    """
    Retorna el job de procesamiento IA.

    Procesa TODOS los artículos pendientes iterando en batches hasta vaciar
    la cola por completo. Entre cada batch espera `batch_delay_seconds` para
    respetar los rate limits de los proveedores LLM gratuitos.

    La configuración del modelo 'worker' se lee desde la BD en cada ejecución,
    por lo que los cambios en la UI toman efecto sin reiniciar el scheduler.

    Args:
        batch_size:           Artículos por llamada al LLM (default 50).
        batch_delay_seconds:  Pausa entre batches en segundos (default 30).
                              Ajustar según el tier del proveedor LLM.
    """
    def _job() -> None:
        logger.info(
            "Job process: iniciando (batch_size=%d, delay=%ds).",
            batch_size, int(batch_delay_seconds),
        )
        publish("job_start", {"job": "process"})
        session = session_factory()
        try:
            cfg = get_model_config(session, "worker")
            if cfg is None:
                logger.warning(
                    "No hay modelo 'worker' configurado en la BD. "
                    "Configura uno en /web/models antes de procesar artículos."
                )
                publish("job_error", {"job": "process", "error": "No hay modelo 'worker' configurado."})
                return

            total_processed = 0
            total_skipped = 0
            batch_num = 0

            while True:
                pending = count_articles(session, processed=False)
                if pending == 0:
                    break

                batch_num += 1
                logger.info(
                    "Job process: batch %d — %d artículos pendientes.", batch_num, pending
                )

                # Contador acumulado al inicio del batch (para calcular pendientes en callback)
                _batch_start_pending = pending

                def _on_article_done(partial_result, _pending=_batch_start_pending):
                    done_so_far = partial_result.processed + partial_result.skipped
                    est_pending = max(0, _pending - done_so_far + (total_processed + total_skipped - (total_processed + total_skipped)))
                    # Emitir progreso con totales acumulados + parciales de este batch
                    publish("process_progress", {
                        "batch": batch_num,
                        "processed": total_processed + partial_result.processed,
                        "skipped": total_skipped + partial_result.skipped,
                        "pending": max(0, _pending - done_so_far),
                    })

                custom_prompt = get_worker_system_prompt(session)
                result = process_pending_articles(
                    session,
                    model=cfg.litellm_model,
                    api_key=cfg.api_key,
                    base_url=cfg.base_url,
                    limit=batch_size,
                    system_prompt=custom_prompt,
                    on_article_done=_on_article_done,
                )
                session.commit()
                total_processed += result.processed
                total_skipped += result.skipped

                remaining = count_articles(session, processed=False)
                if remaining > 0:
                    logger.info(
                        "Job process: batch %d completado (%s). "
                        "%d pendientes — pausa de %ds antes del siguiente batch.",
                        batch_num, result, remaining, int(batch_delay_seconds),
                    )
                    time.sleep(batch_delay_seconds)

            logger.info(
                "Job process: cola vaciada. %d procesados, %d omitidos en %d batches.",
                total_processed, total_skipped, batch_num,
            )
            publish("job_done", {"job": "process", "processed": total_processed, "skipped": total_skipped})

        except Exception as exc:
            logger.exception("Job process: error inesperado: %s", exc)
            session.rollback()
            publish("job_error", {"job": "process", "error": str(exc)})
        finally:
            session.close()

    return _job


def make_briefing_job(
    session_factory: Callable[[], Session],
    audio_dir: str = "data/audio",
) -> Callable[[], None]:
    """
    Retorna el job de generación del briefing diario.

    Después de guardar el briefing, intenta generar el audio TTS si hay
    un proveedor configurado en la BD. Si el TTS falla, el briefing queda
    guardado igualmente — el audio es opcional.
    """
    def _job() -> None:
        logger.info("Job briefing: iniciando.")
        publish("job_start", {"job": "briefing"})
        session = session_factory()
        try:
            run_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            briefing = generate_daily_briefing(session, date.today(), run_at=run_at)
            session.commit()
            if not briefing:
                logger.warning("Job briefing: no se generó ningún briefing.")
                publish("job_error", {"job": "briefing", "error": "Sin artículos procesados para generar el briefing."})
                return

            logger.info("Job briefing: briefing %s (run_at=%s) generado.", briefing.date, briefing.run_at)
            publish("job_done", {"job": "briefing", "date": str(briefing.date)})

            # ── TTS: generar audio si hay proveedor configurado ───────────────
            try:
                tts_cfg = get_tts_config(session)
                provider = tts_cfg.get("tts_provider", "disabled")

                if provider in ("openai", "elevenlabs"):
                    from pathlib import Path
                    from app.tts.generate import generate_audio_for_briefing

                    api_key = tts_cfg.get(f"tts_{provider}_api_key", "")
                    if not api_key:
                        logger.warning(
                            "Job briefing: TTS %s configurado pero sin API key — omitiendo audio.",
                            provider,
                        )
                    else:
                        voice_key = "tts_openai_voice" if provider == "openai" else "tts_elevenlabs_voice_id"
                        voice = tts_cfg.get(voice_key, "")

                        kwargs: dict = {}
                        if provider == "openai":
                            kwargs["openai_model"] = tts_cfg.get("tts_openai_model", "tts-1-hd")

                        # Derivar nombre del archivo de audio desde run_at
                        # "2024-01-15 06:00" → "briefing-2024-01-15-0600.mp3"
                        run_at_slug = briefing.run_at.replace(" ", "-").replace(":", "")
                        filename = f"briefing-{run_at_slug}.mp3"
                        output_path = Path(audio_dir) / filename

                        generate_audio_for_briefing(
                            text=briefing.headlines_text or "",
                            output_path=output_path,
                            provider=provider,
                            api_key=api_key,
                            voice=voice,
                            **kwargs,
                        )
                        update_briefing_audio(session, briefing.id, filename)
                        session.commit()
                        logger.info("Job briefing: audio TTS guardado → %s", filename)

            except Exception as tts_exc:
                logger.warning(
                    "Job briefing: TTS falló (briefing guardado sin audio): %s", tts_exc
                )

        except Exception as exc:
            logger.exception("Job briefing: error inesperado: %s", exc)
            session.rollback()
            publish("job_error", {"job": "briefing", "error": str(exc)})
        finally:
            session.close()

    return _job


# ─── Ensamble del scheduler ───────────────────────────────────────────────────

def _parse_hhmm(time_str: str) -> tuple[int, int]:
    """Convierte "HH:MM" a (hour, minute). Lanza ValueError si el formato es inválido."""
    try:
        hour, minute = time_str.strip().split(":")
        return int(hour), int(minute)
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"DAILY_FETCH_TIME debe estar en formato HH:MM, recibido: {time_str!r}"
        ) from exc


def _add_pipeline_slot(
    scheduler: BackgroundScheduler,
    slot_num: int,
    fetch_h: int,
    fetch_m: int,
    session_factory: Callable[[], Session],
    sources_config_path: str,
    audio_dir: str,
) -> None:
    """
    Agrega los 3 jobs de un slot de pipeline al scheduler.

    IDs: pipeline_{slot_num}_fetch, pipeline_{slot_num}_process, pipeline_{slot_num}_briefing
    """
    def _add_hours(h: int, m: int, delta_hours: int) -> tuple[int, int]:
        total_minutes = h * 60 + m + delta_hours * 60
        return (total_minutes // 60) % 24, total_minutes % 60

    process_h, process_m = _add_hours(fetch_h, fetch_m, 1)
    briefing_h, briefing_m = _add_hours(fetch_h, fetch_m, 2)

    scheduler.add_job(
        make_fetch_job(session_factory, sources_config_path),
        trigger=CronTrigger(hour=fetch_h, minute=fetch_m),
        id=f"pipeline_{slot_num}_fetch",
        name=f"Fetch diario #{slot_num}",
        replace_existing=True,
    )
    scheduler.add_job(
        make_process_job(session_factory),
        trigger=CronTrigger(hour=process_h, minute=process_m),
        id=f"pipeline_{slot_num}_process",
        name=f"Procesamiento IA #{slot_num}",
        replace_existing=True,
    )
    scheduler.add_job(
        make_briefing_job(session_factory, audio_dir=audio_dir),
        trigger=CronTrigger(hour=briefing_h, minute=briefing_m),
        id=f"pipeline_{slot_num}_briefing",
        name=f"Briefing diario #{slot_num}",
        replace_existing=True,
    )

    logger.info(
        "Slot %d configurado — fetch=%02d:%02d, process=%02d:%02d, briefing=%02d:%02d UTC",
        slot_num, fetch_h, fetch_m, process_h, process_m, briefing_h, briefing_m,
    )


def reschedule_all_pipeline_jobs(
    scheduler: BackgroundScheduler,
    slots: list[str],
    session_factory: Callable[[], Session],
    sources_config_path: str,
    audio_dir: str,
) -> None:
    """
    Elimina todos los jobs con prefijo 'pipeline_' y recrea los slots dados.

    Args:
        scheduler:           El BackgroundScheduler activo.
        slots:               Lista de "HH:MM" — uno por ciclo diario.
        session_factory:     Callable que retorna una nueva Session.
        sources_config_path: Ruta al sources.yaml.
        audio_dir:           Directorio de salida para audios TTS.
    """
    # Eliminar todos los jobs existentes de pipeline
    for job in scheduler.get_jobs():
        if job.id.startswith("pipeline_"):
            scheduler.remove_job(job.id)

    # Agregar los nuevos slots
    for i, slot in enumerate(slots, start=1):
        fetch_h, fetch_m = _parse_hhmm(slot)
        _add_pipeline_slot(scheduler, i, fetch_h, fetch_m, session_factory, sources_config_path, audio_dir)


def create_scheduler(
    slots: list[str],
    session_factory: Callable[[], Session],
    sources_config_path: str,
    audio_dir: str = "data/audio",
) -> BackgroundScheduler:
    """
    Crea y configura el BackgroundScheduler con N ciclos diarios (slots).

    No inicia el scheduler — el caller llama a scheduler.start().

    Cada slot genera 3 jobs:
      - pipeline_{n}_fetch     → fetch_time
      - pipeline_{n}_process   → fetch_time + 1h
      - pipeline_{n}_briefing  → fetch_time + 2h

    Args:
        slots:               Lista de "HH:MM" — hora base de cada ciclo.
        session_factory:     Callable que retorna una nueva Session.
        sources_config_path: Ruta al sources.yaml.
        audio_dir:           Directorio de salida para audios TTS.
    """
    scheduler = BackgroundScheduler(timezone="UTC")

    for i, slot in enumerate(slots, start=1):
        fetch_h, fetch_m = _parse_hhmm(slot)
        _add_pipeline_slot(scheduler, i, fetch_h, fetch_m, session_factory, sources_config_path, audio_dir)

    logger.info("Scheduler configurado con %d slot(s): %s", len(slots), ", ".join(slots))
    return scheduler
