# daily-news

> Tu resumen diario de noticias generado por IA, listo antes de que te levantes.

**daily-news** es un servicio personal que lee tus fuentes de noticias favoritas, las procesa con Claude para generar titulares y resúmenes en español, y entrega el resultado en tres formatos:

- **Google Home / Nest** — feed Atom con texto breve optimizado para lectura en voz alta (TTS)
- **iPhone / lector RSS** — mismo feed con resúmenes completos de cada artículo
- **Agentes de IA** — todos los endpoints expuestos como herramientas MCP en `/mcp`

El ciclo ocurre automáticamente todos los días a la hora que configures: fetch de feeds → procesamiento con Claude → briefing listo.

---

## Cómo funciona

```
                    ┌─────────────────────────────────────────┐
  sources.yaml ──▶  │  Fetch RSS (httpx + feedparser)         │  06:00
                    │  Guarda artículos nuevos en SQLite       │
                    └────────────────┬────────────────────────┘
                                     │
                    ┌────────────────▼────────────────────────┐
                    │  Procesamiento IA (Claude)              │  07:00
                    │  · Titular breve (TTS)                  │
                    │  · Resumen 2-3 oraciones (español)      │
                    │  · Score de relevancia 0.0 – 1.0        │
                    └────────────────┬────────────────────────┘
                                     │
                    ┌────────────────▼────────────────────────┐
                    │  Briefing diario                        │  08:00
                    │  · Top 7 titulares  → Google Home       │
                    │  · Top 15 resúmenes → iPhone / RSS      │
                    └───────┬─────────────────────┬──────────┘
                            │                     │
                     GET /feed.xml          GET /api/v1/...
                     (Atom feed)            (REST + MCP)
```

---

## Requisitos

Solo necesitas **Docker Desktop** con Compose v2 (el plugin integrado, no el `docker-compose` viejo).

- Docker Desktop 4.x o superior
- `make` (incluido en macOS y Linux; en Windows: Git Bash, WSL, o [Make for Windows](https://gnuwin32.sourceforge.net/packages/make.htm))

No se requiere Python, pip, ni nada más en tu máquina local.

---

## Instalación y primer uso

### 1. Clonar y configurar

```bash
git clone <url-del-repo> daily-news
cd daily-news
cp .env.example .env
```

Editar `.env` y completar al menos la API key de Anthropic:

```bash
ANTHROPIC_API_KEY=sk-ant-api03-...
```

### 2. Construir las imágenes

```bash
make build
```

Esto construye dos imágenes:
- `daily-news:latest` — la app de producción (imagen mínima, usuario no-root)
- `daily-news-test:latest` — la imagen de tests con pytest y todas las dependencias de dev

Solo es necesario volver a ejecutar esto si cambian `requirements.txt`, `requirements-dev.txt` o el `Dockerfile`.

### 3. Verificar que todo funciona

```bash
make test
```

Corre las 173 pruebas automatizadas dentro del contenedor. No instala nada en tu máquina.

### 4. Levantar la aplicación

```bash
make up
```

La API queda disponible en [http://localhost:8000](http://localhost:8000).

---

## Configuración

### Variables de entorno (`.env`)

| Variable | Requerida | Default | Descripción |
|---|:---:|---|---|
| `ANTHROPIC_API_KEY` | ✅ | — | API key de [console.anthropic.com](https://console.anthropic.com) |
| `PORT` | | `8000` | Puerto de la aplicación (uvicorn y mapeo Docker) |
| `DAILY_FETCH_TIME` | | `06:00` | Hora del fetch diario (UTC, formato HH:MM) |
| `FEED_TITLE` | | `Daily News Briefing` | Título del feed Atom |
| `FEED_DESCRIPTION` | | `Tu resumen diario de noticias` | Subtítulo del feed |
| `FEED_BASE_URL` | | `http://localhost:8000` | URL pública del servidor (importante para los links del feed) |
| `DATABASE_URL` | | `sqlite:///./data/news.db` | URL de la base de datos |

> **Nota sobre `DAILY_FETCH_TIME`:** el procesamiento IA ocurre 1 hora después y el briefing se genera 2 horas después. Si configurás `06:00`, el briefing queda listo a las `08:00`.

### Fuentes de noticias (`config/sources.yaml`)

Editá este archivo para agregar o quitar fuentes. Los cambios se aplican en el siguiente ciclo sin necesidad de reconstruir la imagen.

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
        url: "https://www.lanacion.com.ar/arc/outboundfeeds/rss/"
        type: rss

  tecnologia:
    label: "Tecnología"
    language: en
    sources:
      - name: "Hacker News"
        url: "https://hnrss.org/frontpage"
        type: rss
```

Para sincronizar las fuentes sin reiniciar la app:

```bash
curl -X POST http://localhost:8000/api/v1/sources/sync
```

---

## Comandos disponibles

```bash
make help          # muestra todos los comandos con descripción
```

### Build

```bash
make build         # construye app + test (con cache)
make build-nc      # reconstruye desde cero sin cache
```

### Tests

```bash
make test                              # suite completa (173 tests)
make test-v                            # verbose, muestra cada test
make test-file F=tests/test_feed.py    # un archivo específico
make test-file F="tests/test_feed.py::TestFeedEndpoint::test_retorna_200"  # un test
make shell                             # bash dentro del contenedor de tests
```

> Los volúmenes montan `app/` y `tests/` del host sobre el contenedor, así que los cambios en el código se reflejan **inmediatamente** sin reconstruir.

### Operación

```bash
make up            # inicia la app en background (http://localhost:8000)
make down          # detiene y elimina los contenedores
make restart       # reinicia (útil después de editar .env o config/)
make logs          # sigue los logs en tiempo real (Ctrl+C para salir)
make clean         # ⚠️  elimina imágenes Y el volumen de la BD
```

---

## Uso

### Feed Atom (Google Home / iPhone)

Agregá esta URL a tu lector de RSS o configura Google Home:

```
http://tu-servidor:8000/feed.xml
```

Parámetros:

| Parámetro | Default | Descripción |
|---|---|---|
| `limit` | `7` | Cantidad de briefings a incluir |

```bash
curl http://localhost:8000/feed.xml
curl http://localhost:8000/feed.xml?limit=14
```

Cada entrada del feed contiene:
- `<summary>` — texto breve numerado, listo para TTS (~90 segundos hablados)
- `<content>` — resúmenes completos de cada artículo para lectura

### API REST

Documentación interactiva (Swagger UI):

```
http://localhost:8000/docs
```

Endpoints principales:

| Método | Endpoint | Descripción |
|---|---|---|
| `GET` | `/api/v1/health` | Estado del servicio |
| `GET` | `/api/v1/briefings/today` | Briefing de hoy |
| `GET` | `/api/v1/briefings/latest` | Briefing más reciente |
| `GET` | `/api/v1/briefings/{YYYY-MM-DD}` | Briefing de una fecha |
| `GET` | `/api/v1/articles` | Artículos (filtrables por fecha y estado) |
| `GET` | `/api/v1/sources` | Fuentes configuradas |
| `POST` | `/api/v1/sources/sync` | Re-sincroniza sources.yaml con la BD |
| `POST` | `/api/v1/jobs/fetch` | Dispara el fetch manualmente (202) |
| `POST` | `/api/v1/jobs/process` | Dispara el procesamiento IA (202) |
| `POST` | `/api/v1/jobs/briefing` | Regenera el briefing de hoy (202) |
| `GET` | `/api/v1/settings` | Configuración actual |
| `PATCH` | `/api/v1/settings/scheduler` | Actualiza la hora del ciclo diario |

### MCP (para agentes de IA)

Todos los endpoints REST están disponibles como herramientas MCP en:

```
http://tu-servidor:8000/mcp
```

---

## Estructura del proyecto

```
daily-news/
├── app/
│   ├── api/
│   │   ├── feed.py          # GET /feed.xml (Atom)
│   │   ├── routes.py        # REST API completa
│   │   └── schemas.py       # Pydantic request/response
│   ├── fetcher/
│   │   ├── rss.py           # fetch async de feeds RSS
│   │   └── sources_loader.py # parsea sources.yaml y sincroniza con BD
│   ├── processor/
│   │   └── claude.py        # análisis de artículos con Claude
│   ├── scheduler/
│   │   └── jobs.py          # jobs APScheduler + generación del briefing
│   ├── storage/
│   │   ├── models.py        # modelos SQLAlchemy (Source, Article, DailyBriefing)
│   │   └── crud.py          # operaciones de BD (SQLAlchemy 2.0)
│   ├── config.py            # Settings (pydantic-settings, .env)
│   └── main.py              # FastAPI app + lifespan + MCP
├── tests/                   # 173 tests (pytest)
├── config/
│   └── sources.yaml         # fuentes de noticias
├── data/                    # SQLite (ignorado por git, volumen Docker)
├── Dockerfile               # multi-stage: base → builder → test → production
├── compose.yaml             # servicios: app (producción) + test
├── Makefile                 # shortcuts para build, test, up, logs, etc.
└── .env.example             # plantilla de configuración
```

---

## Desarrollo

El flujo típico de desarrollo:

```bash
# Editar código en app/ o tests/
# Los volúmenes de compose.yaml sincronizan los cambios automáticamente

make test-v                            # ver qué tests pasan/fallan
make test-file F=tests/test_feed.py    # foco en el módulo que estás cambiando
make shell                             # entrar al contenedor si necesitás debuggear

# Cuando cambian requirements*.txt o el Dockerfile:
make build
```

### Cuándo reconstruir

| Cambio | ¿Rebuild? |
|---|:---:|
| Código en `app/` | No — volumen monta el host |
| Archivos en `tests/` | No — volumen monta el host |
| `config/sources.yaml` | No — volumen monta el host |
| `requirements.txt` | **Sí** — `make build` |
| `requirements-dev.txt` | **Sí** — `make build` |
| `Dockerfile` | **Sí** — `make build` |

---

## Tecnologías

| Componente | Tecnología |
|---|---|
| API | FastAPI + Uvicorn |
| IA | Claude (Anthropic) — `claude-opus-4-6` |
| Feeds RSS | feedparser + httpx (async) |
| Feed Atom | feedgen |
| Base de datos | SQLite + SQLAlchemy 2.0 |
| Scheduler | APScheduler 3.x |
| MCP | fastapi-mcp |
| Tests | pytest + pytest-httpx + pytest-asyncio |
| Contenedores | Docker multi-stage + Compose v2 |
