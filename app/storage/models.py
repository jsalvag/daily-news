"""Modelos de la base de datos (SQLAlchemy)."""

from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    Float,
    ForeignKey,
    create_engine,
)
from sqlalchemy import inspect, text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class Source(Base):
    """Fuente de noticias configurada por el usuario."""

    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    url = Column(String(500), nullable=False, unique=True)
    category = Column(String(100), nullable=False)
    source_type = Column(String(20), default="rss")  # rss | web
    language = Column(String(10), default="es")
    enabled = Column(Boolean, default=True)
    instructions = Column(Text, nullable=True)   # instrucciones LLM para esta fuente
    tags = Column(Text, nullable=True)           # JSON list, ej: '["Argentina","Economía"]'
    created_at = Column(DateTime, default=datetime.utcnow)
    last_fetched_at = Column(DateTime, nullable=True)

    articles = relationship(
        "Article", back_populates="source", cascade="all, delete-orphan"
    )

    @property
    def tags_list(self) -> list[str]:
        """Deserializa el campo tags (JSON) a lista de strings."""
        if not self.tags:
            return []
        try:
            import json
            return json.loads(self.tags)
        except Exception:
            return []

    def __repr__(self) -> str:
        return f"<Source name={self.name!r} category={self.category!r}>"


class Article(Base):
    """Artículo individual de una fuente."""

    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False)
    title = Column(String(500), nullable=False)
    url = Column(String(1000), nullable=False)
    summary = Column(Text, nullable=True)       # resumen original del feed
    content = Column(Text, nullable=True)       # contenido completo si se obtiene
    published_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)
    guid = Column(String(1000), nullable=True)  # identificador único del feed

    # Procesamiento por LLM
    ai_summary = Column(Text, nullable=True)    # resumen generado por LLM
    ai_headline = Column(String(300), nullable=True)  # titular corto
    relevance_score = Column(Float, nullable=True)    # 0.0 - 1.0
    processed = Column(Boolean, default=False)
    tags = Column(Text, nullable=True)          # JSON list, ej: '["Argentina","Inflación"]'

    source = relationship("Source", back_populates="articles")

    @property
    def tags_list(self) -> list[str]:
        """Deserializa el campo tags (JSON) a lista de strings."""
        if not self.tags:
            return []
        try:
            import json
            return json.loads(self.tags)
        except Exception:
            return []

    def __repr__(self) -> str:
        return f"<Article title={self.title[:50]!r} source={self.source_id}>"


class DailyBriefing(Base):
    """Compilado diario generado por el LLM."""

    __tablename__ = "daily_briefings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False)       # YYYY-MM-DD, sin unique (N runs/día)
    run_at = Column(String(16), nullable=False, unique=True, default="")  # YYYY-MM-DD HH:MM
    headlines_text = Column(Text, nullable=True)   # versión corta (Google Home / TTS)
    full_text = Column(Text, nullable=True)         # versión completa (iPhone)
    article_ids = Column(Text, nullable=True)       # IDs separados por coma
    generated_at = Column(DateTime, default=datetime.utcnow)
    audio_filename = Column(String(200), nullable=True)  # nombre del MP3 generado por TTS

    def __repr__(self) -> str:
        return f"<DailyBriefing date={self.date!r} run_at={self.run_at!r}>"


class AIModelConfig(Base):
    """Configuración de modelo de IA para un rol específico (worker o editor)."""

    __tablename__ = "ai_model_configs"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    role       = Column(String(20), nullable=False, unique=True)   # "worker" | "editor"
    provider   = Column(String(50), nullable=False)                # "groq", "anthropic", "openai", "ollama"
    model_id   = Column(String(200), nullable=False)               # "llama3-70b-8192", "claude-opus-4-6"
    api_key    = Column(String(500), nullable=True)                # None para proveedores sin auth (Ollama)
    base_url   = Column(String(500), nullable=True)                # URL base para Ollama / LMStudio
    is_active  = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def litellm_model(self) -> str:
        """Retorna el string de modelo en formato LiteLLM: 'provider/model_id'."""
        return f"{self.provider}/{self.model_id}"

    def __repr__(self) -> str:
        return f"<AIModelConfig role={self.role!r} model={self.litellm_model!r}>"


class AppSetting(Base):
    """Configuración persistente de la aplicación (clave/valor)."""

    __tablename__ = "app_settings"

    key        = Column(String(100), primary_key=True)
    value      = Column(String(500), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<AppSetting {self.key!r}={self.value!r}>"


# ─── Helpers de base de datos ────────────────────────────────────────────────

def create_db_engine(database_url: str):
    """Crea el engine de SQLAlchemy."""
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args)


def init_db(engine) -> None:
    """Crea todas las tablas si no existen y aplica migraciones incrementales."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # ── Migración: daily_briefings → agregar run_at y quitar UNIQUE de date ──
    # SQLite no soporta DROP CONSTRAINT, así que recreamos la tabla completa.
    if "daily_briefings" in existing_tables:
        existing_cols = {c["name"] for c in inspector.get_columns("daily_briefings")}
        if "run_at" not in existing_cols:
            # Necesitamos recrear la tabla para agregar run_at y quitar UNIQUE de date.
            # Detectar si audio_filename existe en la tabla vieja.
            has_audio = "audio_filename" in existing_cols
            audio_select = ", audio_filename" if has_audio else ", NULL AS audio_filename"

            with engine.connect() as conn:
                # 1. Crear tabla nueva con el esquema actualizado
                conn.execute(text("""
                    CREATE TABLE daily_briefings_new (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        date VARCHAR(10) NOT NULL,
                        run_at VARCHAR(16) NOT NULL UNIQUE DEFAULT '',
                        headlines_text TEXT,
                        full_text TEXT,
                        article_ids TEXT,
                        generated_at DATETIME,
                        audio_filename VARCHAR(200)
                    )
                """))
                # 2. Migrar datos: run_at = date || ' 00:00'
                conn.execute(text(f"""
                    INSERT INTO daily_briefings_new
                        (id, date, run_at, headlines_text, full_text, article_ids,
                         generated_at, audio_filename)
                    SELECT id, date, (date || ' 00:00'), headlines_text, full_text,
                           article_ids, generated_at{audio_select}
                    FROM daily_briefings
                """))
                # 3. Eliminar tabla vieja y renombrar la nueva
                conn.execute(text("DROP TABLE daily_briefings"))
                conn.execute(text("ALTER TABLE daily_briefings_new RENAME TO daily_briefings"))
                conn.commit()

    # ── Migración: sources → agregar instructions y tags ──────────────────────
    if "sources" in existing_tables:
        source_cols = {c["name"] for c in inspector.get_columns("sources")}
        with engine.connect() as conn:
            if "instructions" not in source_cols:
                conn.execute(text("ALTER TABLE sources ADD COLUMN instructions TEXT"))
            if "tags" not in source_cols:
                conn.execute(text("ALTER TABLE sources ADD COLUMN tags TEXT"))
            conn.commit()

    # ── Migración: articles → agregar tags ────────────────────────────────────
    if "articles" in existing_tables:
        article_cols = {c["name"] for c in inspector.get_columns("articles")}
        if "tags" not in article_cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE articles ADD COLUMN tags TEXT"))
                conn.commit()

    # Crear tablas que no existen (incluye daily_briefings si es nueva BD)
    Base.metadata.create_all(bind=engine)


def get_session_factory(engine):
    """Retorna una fábrica de sesiones."""
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)
