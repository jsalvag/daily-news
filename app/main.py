"""
Punto de entrada de la aplicación FastAPI.

Lifespan (startup / shutdown):
  1. Carga la configuración desde .env
  2. Inicializa la base de datos (crea tablas si no existen)
  3. Sincroniza sources.yaml → BD
  4. Lee daily_fetch_time desde BD (sobreescribe .env si existe)
  5. Crea y arranca el BackgroundScheduler (APScheduler)
  6. Monta los endpoints MCP vía fastapi-mcp
  7. Monta la Web UI vía Jinja2

El scheduler se apaga limpiamente en el shutdown.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.feed import feed_router
from app.api.routes import router
from app.config import get_settings
from app.fetcher.sources_loader import load_and_sync
from app.scheduler.jobs import create_scheduler
from app.storage.crud import get_app_setting
from app.storage.models import create_db_engine, get_session_factory, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────

    settings = get_settings()
    app.state.settings = settings

    # Base de datos
    engine = create_db_engine(settings.database_url)
    init_db(engine)
    session_factory = get_session_factory(engine)
    app.state.session_factory = session_factory
    logger.info("Base de datos inicializada: %s", settings.database_url)

    # Sincronizar sources.yaml al arrancar (no bloquea si el archivo no existe)
    try:
        db = session_factory()
        result = load_and_sync(settings.sources_config_path, db)
        db.commit()
        db.close()
        logger.info("sources.yaml sincronizado: %s", result)
    except FileNotFoundError:
        logger.warning(
            "sources.yaml no encontrado en '%s' — se omite la sincronización inicial.",
            settings.sources_config_path,
        )
    except Exception as exc:
        logger.error("Error al sincronizar sources.yaml: %s", exc)

    # Leer daily_fetch_time desde BD (tiene prioridad sobre .env)
    try:
        db = session_factory()
        saved_time = get_app_setting(db, "daily_fetch_time")
        db.close()
        if saved_time:
            settings.daily_fetch_time = saved_time
            logger.info("daily_fetch_time cargado desde BD: %s", saved_time)
    except Exception as exc:
        logger.warning("No se pudo leer daily_fetch_time desde BD: %s", exc)

    # Scheduler
    scheduler = create_scheduler(
        daily_fetch_time=settings.daily_fetch_time,
        session_factory=session_factory,
        sources_config_path=settings.sources_config_path,
    )
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("Scheduler iniciado. Fetch diario a las %s UTC.", settings.daily_fetch_time)

    logger.info("daily-news iniciado y listo.")
    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    scheduler.shutdown(wait=False)
    engine.dispose()
    logger.info("daily-news apagado correctamente.")


# ─── Aplicación ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="daily-news",
    description=(
        "API de briefings de noticias personalizados. "
        "Todos los endpoints están disponibles como herramientas MCP en /mcp."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1")
app.include_router(feed_router)   # /feed.xml (sin prefijo)


# ─── Web UI ───────────────────────────────────────────────────────────────────
# Se importa aquí para evitar importaciones circulares en el módulo web.

from app.web.routes import web_router  # noqa: E402

app.include_router(web_router)   # /web/*


# ─── MCP ──────────────────────────────────────────────────────────────────────
# Se monta DESPUÉS de registrar las rutas para que fastapi-mcp las exponga todas.

from fastapi_mcp import FastApiMCP  # noqa: E402  (importación tardía intencional)

mcp = FastApiMCP(app)
mcp.mount()   # Expone el servidor MCP en /mcp
