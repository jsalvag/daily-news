# daily-news — Product Requirements Document

> **Última actualización:** 2026-03-14
> **Estado del proyecto:** En desarrollo activo — Parte 3 de 7 completada

---

## 1. Visión General

**daily-news** es un compilado diario de noticias personalizado que:

- Obtiene y procesa noticias de múltiples fuentes (RSS, blogs, URLs) durante la noche.
- Entrega **titulares cortos** al dispositivo Google Nest/Home en la mañana, como parte de la rutina matutina ("Hey Google, dame las noticias").
- Entrega **artículos completos con resúmenes IA** al iPhone a través de una interfaz web o iOS Shortcut/Siri.
- Expone todos sus endpoints como **herramientas MCP** para que cualquier IA (Claude u otras) pueda consultarlo directamente.

---

## 2. Objetivos

| Objetivo | Descripción |
|----------|-------------|
| Automatización matutina | El usuario recibe noticias relevantes al despertar sin intervención manual |
| Personalización | Categorías y fuentes definidas por el usuario vía `config/sources.yaml` |
| Multi-dispositivo | Google Nest para audio breve; iPhone/web para lectura detallada |
| AI-first | Resúmenes y ranking de relevancia generados por Claude |
| MCP-friendly | Expuesto como herramienta para agentes IA mediante `fastapi-mcp` |
| Portabilidad | Todo corre en Docker; sin contaminación del entorno local |

---

## 3. Categorías de Noticias (configuración inicial)

| Categoría | Descripción | Idioma |
|-----------|-------------|--------|
| `argentina` | Noticias generales de Argentina | es |
| `venezuela` | Política y economía de Venezuela | es |
| `tecnologia` | Noticias de tecnología mundial | en |
| `mercados` | Bolsa, crypto, finanzas | en |

Las categorías y fuentes se definen en `config/sources.yaml` y son editables sin rebuild del contenedor.

---

## 4. Casos de Uso

### 4.1 Rutina matutina — Google Nest
- Google Home llama al endpoint RSS de la app (`/feed/headlines.rss`) como parte de su rutina.
- Lee los 5–7 titulares más relevantes del día en voz alta.
- Duración esperada: ~60–90 segundos.

### 4.2 Consulta on-demand — "Hey Google, dame las noticias"
- Misma llamada al endpoint RSS.
- Disponible en cualquier momento del día.

### 4.3 Lectura detallada — iPhone
- El usuario abre la web UI (`http://<host>:8000`) o usa un iOS Shortcut.
- Ve los artículos del día con resúmenes IA, ordenados por relevancia.
- Puede filtrar por categoría.
- Puede pedir a una IA que profundice en un artículo usando el MCP.

### 4.4 Uso por IA (MCP)
- Un agente IA (ej: Claude en Claude.ai o via API) puede usar la app como herramienta.
- Endpoints disponibles como tools MCP en `/mcp`.
- El agente puede consultar briefings, artículos, o forzar un fetch.

---

## 5. Arquitectura

```
┌─────────────────────────────────────────────────────┐
│                   daily-news                      │
│                                                     │
│  ┌──────────┐    ┌───────────┐    ┌──────────────┐  │
│  │ Scheduler│───▶│  Fetcher  │───▶│   Storage    │  │
│  │(APSched) │    │(RSS+httpx)│    │ (SQLite+SA)  │  │
│  └──────────┘    └───────────┘    └──────┬───────┘  │
│                                          │          │
│  ┌──────────────┐    ┌───────────┐       │          │
│  │  Processor   │◀───│   CRUD    │◀──────┘          │
│  │ (Claude API) │    │           │                   │
│  └──────┬───────┘    └───────────┘                   │
│         │                                            │
│  ┌──────▼───────────────────────────────────────┐   │
│  │              FastAPI App                      │   │
│  │  REST API + MCP (/mcp) + Feed RSS (/feed)     │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
         │                    │                │
    Google Nest           iPhone/Web        Claude AI
    (RSS feed)          (Web UI / iOS)     (MCP tools)
```

### Stack tecnológico

| Capa | Tecnología |
|------|------------|
| Web framework | FastAPI 0.115.x |
| MCP integration | `fastapi-mcp` 0.3.x (auto-expone endpoints en `/mcp`) |
| RSS parsing | `feedparser` 6.0.x |
| HTTP client | `httpx` 0.28.x (async, con semáforo de concurrencia) |
| ORM / DB | SQLAlchemy 2.0 + SQLite |
| AI | Claude API (`claude-opus-4-6`, adaptive thinking) |
| Scheduler | APScheduler 3.10.x |
| RSS generation | `feedgen` 1.0.x |
| Config | `pydantic-settings` + `.env` |
| Source config | PyYAML (`yaml.safe_load`) |
| Contenedores | Docker multi-stage (base → builder → test → production) |

---

## 6. Modelo de Datos

### `sources` — Fuentes de noticias
| Campo | Tipo | Descripción |
|-------|------|-------------|
| id | INTEGER PK | |
| name | TEXT | Nombre legible |
| url | TEXT UNIQUE | URL del feed |
| category | TEXT | Categoría (argentina, tecnologia, …) |
| source_type | TEXT | "rss" (futuro: "scraper", "api") |
| language | TEXT | "es" / "en" |
| enabled | BOOLEAN | Si se fetcha |
| created_at | DATETIME | |
| last_fetched_at | DATETIME | Último fetch exitoso |

### `articles` — Artículos individuales
| Campo | Tipo | Descripción |
|-------|------|-------------|
| id | INTEGER PK | |
| source_id | FK → sources | |
| title | TEXT | Título original |
| url | TEXT | URL del artículo |
| guid | TEXT | Identificador único del feed |
| summary | TEXT | Extracto del feed |
| content | TEXT | Contenido completo (si disponible) |
| published_at | DATETIME | Fecha de publicación |
| fetched_at | DATETIME | Cuándo fue descargado |
| ai_headline | TEXT | Titular generado por Claude |
| ai_summary | TEXT | Resumen generado por Claude |
| relevance_score | FLOAT | 0.0–1.0, generado por Claude |
| processed | BOOLEAN | Si Claude lo procesó |

### `daily_briefings` — Compilados diarios
| Campo | Tipo | Descripción |
|-------|------|-------------|
| id | INTEGER PK | |
| date | TEXT UNIQUE | Formato "YYYY-MM-DD" |
| headlines_text | TEXT | Titulares para Google Nest (voz) |
| full_text | TEXT | Briefing completo para iPhone |
| article_ids | TEXT | IDs de artículos incluidos (CSV) |
| generated_at | DATETIME | |

---

## 7. Fuentes de Noticias (configuración inicial)

Definidas en `config/sources.yaml`, editables sin rebuild:

```yaml
categories:
  argentina:
    label: "Argentina"
    language: es
    sources:
      - name: "Infobae"
        url: "https://www.infobae.com/feeds/rss/"
        type: rss
      - name: "La Nación"
        url: "https://www.lanacion.com.ar/rss/"
        type: rss
      - name: "Clarín"
        url: "https://www.clarin.com/rss/lo-ultimo/"
        type: rss

  venezuela:
    label: "Venezuela"
    language: es
    sources:
      - name: "Efecto Cocuyo"
        url: "https://efectococuyo.com/feed/"
        type: rss
      - name: "El Pitazo"
        url: "https://elpitazo.net/feed/"
        type: rss

  tecnologia:
    label: "Tecnología"
    language: en
    sources:
      - name: "TechCrunch"
        url: "https://techcrunch.com/feed/"
        type: rss
      - name: "The Verge"
        url: "https://www.theverge.com/rss/index.xml"
        type: rss
      - name: "Ars Technica"
        url: "https://feeds.arstechnica.com/arstechnica/index"
        type: rss

  mercados:
    label: "Mercados"
    language: en
    sources:
      - name: "Reuters Business"
        url: "https://feeds.reuters.com/reuters/businessNews"
        type: rss
      - name: "CoinDesk"
        url: "https://www.coindesk.com/arc/outboundfeeds/rss/"
        type: rss
```

---

## 8. API Endpoints

Todos los endpoints se exponen también como MCP tools en `/mcp` automáticamente vía `fastapi-mcp`.

### Salud y status
| Método | Path | Descripción |
|--------|------|-------------|
| GET | `/health` | Health check (usado por Docker) |
| GET | `/` | Info general de la app |

### Fuentes
| Método | Path | Descripción |
|--------|------|-------------|
| GET | `/sources` | Lista todas las fuentes |
| GET | `/sources/{id}` | Detalle de una fuente |
| PATCH | `/sources/{id}/toggle` | Habilitar/deshabilitar fuente |
| POST | `/sources/sync` | Re-sincronizar `sources.yaml` → BD |

### Artículos
| Método | Path | Descripción |
|--------|------|-------------|
| GET | `/articles` | Artículos (filtro: `date`, `category`, `processed`) |
| GET | `/articles/{id}` | Detalle de un artículo |

### Briefings
| Método | Path | Descripción |
|--------|------|-------------|
| GET | `/briefings/today` | Briefing del día actual |
| GET | `/briefings/{date}` | Briefing de una fecha específica |
| POST | `/briefings/generate` | Forzar generación de briefing |

### Feed RSS (Google Nest)
| Método | Path | Descripción |
|--------|------|-------------|
| GET | `/feed/headlines.rss` | Feed RSS con titulares del día para Google Home |

### Jobs manuales
| Método | Path | Descripción |
|--------|------|-------------|
| POST | `/jobs/fetch` | Forzar fetch de todos los feeds ahora |
| POST | `/jobs/process` | Forzar procesamiento IA de artículos pendientes |

---

## 9. Plan de Desarrollo — Estado actual

### ✅ Parte 1 — Base del proyecto
- [x] Estructura de carpetas
- [x] `Dockerfile` multi-stage (base → builder → test → production)
- [x] `compose.yaml` (nombre moderno)
- [x] `requirements.txt` y `requirements-dev.txt`
- [x] `app/config.py` — Settings con pydantic-settings
- [x] `.env.example`

### ✅ Parte 2 — Capa de datos
- [x] `app/storage/models.py` — Modelos SQLAlchemy 2.0 (Source, Article, DailyBriefing)
- [x] `tests/test_storage.py` — 11 tests de modelos y DB

### ✅ Parte 3 — Fetcher y CRUD
- [x] `app/fetcher/rss.py` — Fetcher RSS/Atom asíncrono con httpx
- [x] `app/fetcher/sources_loader.py` — Carga y sincroniza `sources.yaml` → BD
- [x] `app/storage/crud.py` — CRUD SQLAlchemy 2.0 (Sources, Articles, Briefings)
- [x] `tests/test_fetcher_rss.py` — 14 tests (mocks con pytest-httpx)
- [x] `tests/test_sources_loader.py` — 18 tests (YAML parsing + DB sync)
- [x] `tests/test_crud.py` — Tests de todas las operaciones CRUD

### 🔜 Parte 4 — Procesador IA (Claude)
- [ ] `app/processor/claude.py` — Integración con Claude API
  - Resumir artículos individuales
  - Generar titulares en español optimizados para voz
  - Asignar puntuación de relevancia (0.0–1.0) según categoría
  - Modelo: `claude-opus-4-6` con adaptive thinking

### 🔜 Parte 5 — Scheduler
- [ ] `app/scheduler/jobs.py` — Jobs con APScheduler
  - Fetch diario: 03:00 AM (antes del briefing matutino)
  - Procesamiento IA: 04:00 AM
  - Generación de briefing: 05:00 AM

### 🔜 Parte 6 — API FastAPI + MCP
- [ ] `app/main.py` — Entry point con lifespan (init DB, start scheduler, mount MCP)
- [ ] `app/api/routes.py` — Todos los endpoints REST
- [ ] Integración `fastapi-mcp` en 2 líneas: `FastApiMCP(app).mount_http()`

### 🔜 Parte 7 — Feed RSS para Google Home
- [ ] `app/api/feed.py` — Generación de feed RSS con `feedgen`
  - Titulares concisos (máx. 15 palabras) optimizados para TTS
  - Ordenados por relevancia
  - Accesible en `/feed/headlines.rss`

---

## 10. Infraestructura Docker

### Targets del Dockerfile
| Stage | Base | Propósito |
|-------|------|-----------|
| `base` | python:3.12-slim | Runtime: libxml2, libxslt1.1, curl |
| `builder` | base | Compilación: gcc, headers, venv con deps producción |
| `test` | builder | Tests: deps dev + pytest + coverage |
| `production` | base | Final: venv copiado, usuario no-root `app` |

### Comandos clave
```bash
# Correr tests
docker compose --profile test up tests

# Correr producción
docker compose up app

# Build manual de tests
docker build --target test -t daily-news-test .
docker run --rm daily-news-test
```

### Volúmenes
| Volumen | Tipo | Propósito |
|---------|------|-----------|
| `news-data` | Named volume | SQLite persistente (`/app/data`) |
| `./config` | Bind mount (ro) | `sources.yaml` editable sin rebuild |
| `./tests` | Bind mount | Editar tests sin rebuild (solo en `test`) |
| `./app` | Bind mount | Editar código sin rebuild (solo en `test`) |

---

## 11. Variables de Entorno

```bash
# Requeridas
ANTHROPIC_API_KEY=sk-ant-...

# Opcionales (con defaults)
HOST=0.0.0.0
PORT=8000
DATABASE_URL=sqlite:///./data/news.db
DAILY_FETCH_TIME=06:00
SOURCES_CONFIG_PATH=config/sources.yaml

# Feed RSS (Google Home)
FEED_TITLE="Daily News Me"
FEED_DESCRIPTION="Noticias diarias personalizadas"
FEED_BASE_URL=http://localhost:8000
```

---

## 12. Decisiones de Diseño

| Decisión | Alternativa rechazada | Razón |
|----------|----------------------|-------|
| Un solo `Dockerfile` multi-stage | `Dockerfile` + `Dockerfile.test` separados | Una app robusta usa el mismo Dockerfile en todos los entornos |
| `compose.yaml` | `docker-compose.yml` | Nombre moderno; `docker-compose.yml` es obsoleto |
| `fastapi-mcp` | MCP server separado | 2 líneas de código, zero overhead, todos los endpoints se exponen automáticamente |
| `yaml.safe_load()` | `yaml.load()` | `yaml.load()` permite ejecución arbitraria de código; `safe_load` es siempre la opción correcta |
| SQLAlchemy 2.0 API | `session.query()` legacy | API moderna y recomendada; legacy marcado para deprecación |
| `feedparser` en executor | feedparser async propio | feedparser es sync y estable; run_in_executor evita bloquear el event loop |
| SQLite + volumen Docker | PostgreSQL | Simplicidad para uso personal; sin servidor de BD separado |
| Usuario no-root en Docker | root | Buena práctica de seguridad en contenedores |

---

## 13. Convenciones de Código

- **Sin `session.commit()`** en funciones CRUD — el caller decide cuándo commitear.
- **SQLAlchemy 2.0**: siempre `select()` + `session.execute().scalars()`, nunca `session.query()`.
- **PyYAML**: siempre `yaml.safe_load()`, nunca `yaml.load()`.
- **Tests**: cada módulo tiene su archivo de test correspondiente en `tests/`.
- **Fixtures de test**: siempre `sqlite:///:memory:` para isolación.
- **Docker**: bind mounts para `app/` y `tests/` en el stage `test` para editar sin rebuild.
- **Imports**: `from __future__ import annotations` en todos los módulos Python.
