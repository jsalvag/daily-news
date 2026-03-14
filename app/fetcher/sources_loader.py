"""
Carga y sincroniza las fuentes definidas en config/sources.yaml con la BD.

El archivo YAML es la fuente de verdad para la configuración de fuentes.
Este módulo lee el YAML y asegura que la BD refleje el estado actual:
  - Fuentes nuevas en el YAML → se insertan en la BD
  - Fuentes ya existentes (por URL) → se actualizan nombre/categoría/lenguaje
  - Fuentes que dejaron de estar en el YAML → NO se eliminan (pueden tener
    artículos asociados), solo quedan deshabilitadas si el usuario lo decide.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.storage.models import Source

logger = logging.getLogger(__name__)


# ─── Schema del YAML ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceEntry:
    """Representa una entrada individual dentro de una categoría del YAML."""
    name: str
    url: str
    category: str
    source_type: str = "rss"
    language: str = "es"


class SourcesConfigError(ValueError):
    """Error de validación del archivo de configuración de fuentes."""


# ─── Parser del YAML ──────────────────────────────────────────────────────────

def load_sources_from_yaml(path: str | Path) -> list[SourceEntry]:
    """
    Lee `sources.yaml` y retorna la lista plana de fuentes configuradas.

    Estructura esperada del YAML:
        categories:
          argentina:
            label: "Argentina"
            language: es
            sources:
              - name: "Infobae"
                url: "https://..."
                type: rss

    Args:
        path: Ruta al archivo YAML.

    Returns:
        Lista de SourceEntry validadas.

    Raises:
        FileNotFoundError: Si el archivo no existe.
        SourcesConfigError: Si la estructura del YAML es inválida.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Archivo de fuentes no encontrado: {path}")

    with path.open("r", encoding="utf-8") as f:
        # safe_load previene ejecución arbitraria de código Python en el YAML
        raw: Any = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise SourcesConfigError("El YAML debe ser un diccionario en el nivel raíz.")

    categories: Any = raw.get("categories")
    if not isinstance(categories, dict):
        raise SourcesConfigError("El YAML debe contener una clave 'categories' con un diccionario.")

    entries: list[SourceEntry] = []

    for cat_key, cat_data in categories.items():
        if not isinstance(cat_data, dict):
            raise SourcesConfigError(f"Categoría '{cat_key}' debe ser un diccionario.")

        language: str = cat_data.get("language", "es")
        sources_list: Any = cat_data.get("sources", [])

        if not isinstance(sources_list, list):
            raise SourcesConfigError(
                f"Categoría '{cat_key}': 'sources' debe ser una lista."
            )

        for i, src in enumerate(sources_list):
            if not isinstance(src, dict):
                raise SourcesConfigError(
                    f"Categoría '{cat_key}', fuente #{i}: debe ser un diccionario."
                )

            name: str | None = src.get("name")
            url: str | None = src.get("url")

            if not name or not isinstance(name, str):
                raise SourcesConfigError(
                    f"Categoría '{cat_key}', fuente #{i}: 'name' es requerido y debe ser string."
                )
            if not url or not isinstance(url, str):
                raise SourcesConfigError(
                    f"Categoría '{cat_key}', fuente #{i} ('{name}'): 'url' es requerido y debe ser string."
                )

            entries.append(SourceEntry(
                name=name.strip(),
                url=url.strip(),
                category=cat_key,
                source_type=src.get("type", "rss"),
                language=language,
            ))

    logger.debug("YAML parseado: %d fuentes en %d categorías.", len(entries), len(categories))
    return entries


# ─── Sincronización con la BD ─────────────────────────────────────────────────

@dataclass
class SyncResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def __str__(self) -> str:
        return (
            f"Sync: {self.inserted} nuevas, "
            f"{self.updated} actualizadas, "
            f"{self.unchanged} sin cambios"
        )


def sync_sources_to_db(entries: list[SourceEntry], session: Session) -> SyncResult:
    """
    Sincroniza la lista de SourceEntry con la tabla `sources` de la BD.

    Estrategia (upsert manual):
      - Si la URL no existe → INSERT
      - Si la URL existe → UPDATE nombre/categoría/lenguaje/tipo si cambiaron

    Args:
        entries: Lista de fuentes parseadas del YAML.
        session: Sesión SQLAlchemy activa (el caller hace commit).

    Returns:
        SyncResult con contadores de operaciones.
    """
    result = SyncResult()

    # Cargar todas las URLs existentes en un dict para lookups O(1)
    # SQLAlchemy 2.0: select() + scalars() en vez de session.query() (legacy)
    existing: dict[str, Source] = {
        src.url: src
        for src in session.execute(select(Source)).scalars().all()
    }

    for entry in entries:
        if entry.url not in existing:
            # Fuente nueva → insertar
            new_source = Source(
                name=entry.name,
                url=entry.url,
                category=entry.category,
                source_type=entry.source_type,
                language=entry.language,
                enabled=True,
            )
            session.add(new_source)
            result.inserted += 1
            logger.info("Nueva fuente: %r (%s)", entry.name, entry.url)
        else:
            # Fuente existente → actualizar campos si cambiaron
            src = existing[entry.url]
            changed = False

            if src.name != entry.name:
                src.name = entry.name
                changed = True
            if src.category != entry.category:
                src.category = entry.category
                changed = True
            if src.language != entry.language:
                src.language = entry.language
                changed = True
            if src.source_type != entry.source_type:
                src.source_type = entry.source_type
                changed = True

            if changed:
                result.updated += 1
                logger.info("Fuente actualizada: %r", entry.url)
            else:
                result.unchanged += 1

    return result


def load_and_sync(yaml_path: str | Path, session: Session) -> SyncResult:
    """
    Combina `load_sources_from_yaml` + `sync_sources_to_db` en un solo paso.
    El caller es responsable de hacer session.commit() después.

    Args:
        yaml_path: Ruta al archivo de configuración.
        session:   Sesión SQLAlchemy activa.

    Returns:
        SyncResult con el resumen de la operación.
    """
    entries = load_sources_from_yaml(yaml_path)
    result = sync_sources_to_db(entries, session)
    logger.info(str(result))
    return result
