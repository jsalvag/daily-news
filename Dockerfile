# =============================================================================
# daily-news — Dockerfile multi-stage
#
# Stages:
#   base        → imagen base con dependencias de runtime del SO
#   builder     → compila dependencias Python en un venv aislado
#   test        → agrega deps de dev y corre pytest
#   production  → imagen final mínima, usuario no-root, sin build tools
#
# Uso:
#   Producción : docker build --target production -t daily-news .
#   Tests      : docker build --target test       -t daily-news-test .
#   (compose.yaml maneja esto automáticamente por servicio)
# =============================================================================


# ── Stage 1: base ─────────────────────────────────────────────────────────────
# Runtime del SO — solo lo que la app necesita para EJECUTARSE.
# gcc y headers NO van aquí; solo las .so de runtime (lxml las necesita).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libxml2 / libxslt → runtime de lxml (no los headers de compilación)
# curl → healthcheck en compose.yaml
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app


# ── Stage 2: builder ──────────────────────────────────────────────────────────
# Instala dependencias de producción en un venv aislado.
# gcc y headers solo existen en este stage; no "contaminan" la imagen final.
FROM base AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libxml2-dev \
        libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

# Crear venv explícito — facilita copiarlo entre stages
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copiar solo requirements para aprovechar cache de capas:
# si el código cambia pero requirements.txt no, esta capa no se reconstruye.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ── Stage 3: test ─────────────────────────────────────────────────────────────
# Hereda del builder (ya tiene gcc por si algún dep de dev lo necesita)
# y agrega pytest + mocks. CMD corre los tests directamente.
FROM builder AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

# Copiar todo el código (tests incluidos)
COPY . .

CMD ["pytest", "tests/", "-v", "--tb=short", "--cov=app", "--cov-report=term-missing"]


# ── Stage 4: production ───────────────────────────────────────────────────────
# Imagen final: base (sin gcc) + venv copiado + código + usuario no-root.
# Resultado: imagen pequeña, sin herramientas de compilación, menor superficie de ataque.
FROM base AS production

# Usuario no-root — buena práctica de seguridad en contenedores
RUN groupadd --system app \
    && useradd --system --gid app --no-create-home app

# Copiar solo el venv ya compilado desde builder (sin gcc ni headers)
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copiar código con ownership correcto desde el principio
COPY --chown=app:app . .

# Carpeta de datos persistente; audio va dentro del named volume donde app tiene permisos
RUN mkdir -p /app/data /app/data/audio && chown -R app:app /app/data

USER app

EXPOSE 8000

# exec form (sin shell) → PID 1 recibe señales correctamente (SIGTERM graceful shutdown)
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
