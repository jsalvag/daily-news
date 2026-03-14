# Makefile — daily-news
# Requiere Docker con Compose v2 (plugin integrado).
# No requiere Python, pip ni nada instalado localmente.

.PHONY: help build test test-v test-file up down restart logs shell clean

# ── Ayuda ─────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  daily-news — comandos disponibles"
	@echo ""
	@echo "  Primer uso:"
	@echo "    cp .env.example .env   # completar ANTHROPIC_API_KEY"
	@echo "    make build             # construye las imágenes"
	@echo "    make test              # verifica que todo funciona"
	@echo "    make up                # levanta la API"
	@echo ""
	@echo "  Desarrollo:"
	@echo "    make test              # suite completa"
	@echo "    make test-v            # suite con output verbose"
	@echo "    make test-file F=tests/test_feed.py   # un módulo específico"
	@echo "    make shell             # bash dentro del contenedor de tests"
	@echo ""
	@echo "  Operación:"
	@echo "    make up                # inicia la app (background)"
	@echo "    make down              # detiene y elimina contenedores"
	@echo "    make restart           # reinicia la app"
	@echo "    make logs              # sigue los logs de la app"
	@echo "    make clean             # elimina imágenes y volúmenes (¡borra la BD!)"
	@echo ""

# ── Build ─────────────────────────────────────────────────────────────────────

## Construye (o reconstruye) las imágenes app y test.
## Necesario al cambiar requirements*.txt o el Dockerfile.
build:
	docker compose build
	docker compose --profile test build

## Reconstruye sin usar cache (útil si algo quedó roto).
build-nc:
	docker compose build --no-cache
	docker compose --profile test build --no-cache

# ── Tests ─────────────────────────────────────────────────────────────────────

## Corre la suite completa de tests.
test:
	docker compose --profile test run --rm test

## Suite completa con output verbose (muestra cada test individualmente).
test-v:
	docker compose --profile test run --rm test \
	  pytest tests/ -v --tb=short

## Corre un módulo o archivo específico.
## Uso: make test-file F=tests/test_feed.py
##      make test-file F="tests/test_feed.py::TestFeedEndpoint::test_retorna_200"
test-file:
	@if [ -z "$(F)" ]; then echo "Uso: make test-file F=tests/test_foo.py"; exit 1; fi
	docker compose --profile test run --rm test pytest $(F) -v --tb=short

## Shell interactivo dentro del contenedor de tests (para debug).
shell:
	docker compose --profile test run --rm --entrypoint bash test

# ── Operación ─────────────────────────────────────────────────────────────────

## Inicia la API en background (http://localhost:8000).
up:
	docker compose up app -d

## Detiene y elimina los contenedores (los datos en db_data se conservan).
down:
	docker compose down

## Reinicia la app (útil después de cambiar .env o config/).
restart:
	docker compose restart app

## Sigue los logs de la app en tiempo real (Ctrl+C para salir).
logs:
	docker compose logs -f app

# ── Limpieza ──────────────────────────────────────────────────────────────────

## ¡PRECAUCIÓN! Elimina imágenes y el volumen de la BD (datos permanentes borrados).
clean:
	docker compose down --volumes --rmi all
