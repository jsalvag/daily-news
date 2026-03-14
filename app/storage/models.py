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
    created_at = Column(DateTime, default=datetime.utcnow)
    last_fetched_at = Column(DateTime, nullable=True)

    articles = relationship(
        "Article", back_populates="source", cascade="all, delete-orphan"
    )

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

    # Procesamiento por Claude
    ai_summary = Column(Text, nullable=True)    # resumen generado por Claude
    ai_headline = Column(String(300), nullable=True)  # titular corto
    relevance_score = Column(Float, nullable=True)    # 0.0 - 1.0
    processed = Column(Boolean, default=False)

    source = relationship("Source", back_populates="articles")

    def __repr__(self) -> str:
        return f"<Article title={self.title[:50]!r} source={self.source_id}>"


class DailyBriefing(Base):
    """Compilado diario generado por Claude."""

    __tablename__ = "daily_briefings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, unique=True)  # YYYY-MM-DD
    headlines_text = Column(Text, nullable=True)   # versión corta (Google Home)
    full_text = Column(Text, nullable=True)         # versión completa (iPhone)
    article_ids = Column(Text, nullable=True)       # IDs separados por coma
    generated_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<DailyBriefing date={self.date!r}>"


# ─── Helpers de base de datos ────────────────────────────────────────────────

def create_db_engine(database_url: str):
    """Crea el engine de SQLAlchemy."""
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args)


def init_db(engine) -> None:
    """Crea todas las tablas si no existen."""
    Base.metadata.create_all(bind=engine)


def get_session_factory(engine):
    """Retorna una fábrica de sesiones."""
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)
