"""
Tests para el loader de fuentes (YAML → BD).
"""

import textwrap
from pathlib import Path

import pytest

from app.fetcher.sources_loader import (
    SourceEntry,
    SourcesConfigError,
    SyncResult,
    load_sources_from_yaml,
    sync_sources_to_db,
    load_and_sync,
)
from app.storage.models import Source, create_db_engine, init_db, get_session_factory


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def session():
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    Session = get_session_factory(engine)
    db = Session()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture
def valid_yaml(tmp_path: Path) -> Path:
    content = textwrap.dedent("""
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

          tecnologia:
            label: "Tecnología"
            language: en
            sources:
              - name: "TechCrunch"
                url: "https://techcrunch.com/feed/"
                type: rss
    """)
    f = tmp_path / "sources.yaml"
    f.write_text(content, encoding="utf-8")
    return f


@pytest.fixture
def minimal_yaml(tmp_path: Path) -> Path:
    content = textwrap.dedent("""
        categories:
          test:
            language: es
            sources:
              - name: "Test Source"
                url: "https://test.com/rss"
                type: rss
    """)
    f = tmp_path / "sources.yaml"
    f.write_text(content, encoding="utf-8")
    return f


# ─── Tests: load_sources_from_yaml ───────────────────────────────────────────

class TestLoadSourcesFromYaml:
    def test_parsea_yaml_valido(self, valid_yaml):
        entries = load_sources_from_yaml(valid_yaml)
        assert len(entries) == 3

    def test_asigna_categoria_correctamente(self, valid_yaml):
        entries = load_sources_from_yaml(valid_yaml)
        arg_entries = [e for e in entries if e.category == "argentina"]
        tech_entries = [e for e in entries if e.category == "tecnologia"]
        assert len(arg_entries) == 2
        assert len(tech_entries) == 1

    def test_hereda_language_de_categoria(self, valid_yaml):
        entries = load_sources_from_yaml(valid_yaml)
        techcrunch = next(e for e in entries if e.name == "TechCrunch")
        infobae = next(e for e in entries if e.name == "Infobae")
        assert techcrunch.language == "en"
        assert infobae.language == "es"

    def test_campos_correctos(self, valid_yaml):
        entries = load_sources_from_yaml(valid_yaml)
        infobae = next(e for e in entries if e.name == "Infobae")
        assert infobae.url == "https://www.infobae.com/feeds/rss/"
        assert infobae.source_type == "rss"
        assert isinstance(infobae, SourceEntry)

    def test_archivo_no_existente_lanza_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_sources_from_yaml(tmp_path / "noexiste.yaml")

    def test_yaml_sin_categories_lanza_error(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("foo: bar", encoding="utf-8")
        with pytest.raises(SourcesConfigError, match="categories"):
            load_sources_from_yaml(f)

    def test_yaml_no_es_dict_lanza_error(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("- item1\n- item2", encoding="utf-8")
        with pytest.raises(SourcesConfigError):
            load_sources_from_yaml(f)

    def test_fuente_sin_name_lanza_error(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text(textwrap.dedent("""
            categories:
              test:
                language: es
                sources:
                  - url: "https://x.com/rss"
                    type: rss
        """), encoding="utf-8")
        with pytest.raises(SourcesConfigError, match="name"):
            load_sources_from_yaml(f)

    def test_fuente_sin_url_lanza_error(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text(textwrap.dedent("""
            categories:
              test:
                language: es
                sources:
                  - name: "Algo"
                    type: rss
        """), encoding="utf-8")
        with pytest.raises(SourcesConfigError, match="url"):
            load_sources_from_yaml(f)

    def test_type_defecto_es_rss(self, tmp_path):
        """Si no se especifica type, debe usar 'rss'."""
        f = tmp_path / "sources.yaml"
        f.write_text(textwrap.dedent("""
            categories:
              test:
                language: es
                sources:
                  - name: "Sin tipo"
                    url: "https://x.com/rss"
        """), encoding="utf-8")
        entries = load_sources_from_yaml(f)
        assert entries[0].source_type == "rss"


# ─── Tests: sync_sources_to_db ───────────────────────────────────────────────

class TestSyncSourcesToDb:
    def _entries(self) -> list[SourceEntry]:
        return [
            SourceEntry("Infobae", "https://infobae.com/rss", "argentina"),
            SourceEntry("TechCrunch", "https://techcrunch.com/feed", "tecnologia", language="en"),
        ]

    def test_inserta_fuentes_nuevas(self, session):
        result = sync_sources_to_db(self._entries(), session)
        session.commit()
        assert result.inserted == 2
        assert result.updated == 0
        assert result.unchanged == 0

    def test_no_duplica_en_segunda_sync(self, session):
        sync_sources_to_db(self._entries(), session)
        session.commit()

        # Segunda sync con los mismos datos
        result = sync_sources_to_db(self._entries(), session)
        session.commit()

        assert result.inserted == 0
        assert result.unchanged == 2

    def test_actualiza_nombre_cambiado(self, session):
        sync_sources_to_db(self._entries(), session)
        session.commit()

        # Cambiamos el nombre de Infobae
        modified = [
            SourceEntry("Infobae.com", "https://infobae.com/rss", "argentina"),
            SourceEntry("TechCrunch", "https://techcrunch.com/feed", "tecnologia", language="en"),
        ]
        result = sync_sources_to_db(modified, session)
        session.commit()

        assert result.updated == 1
        assert result.unchanged == 1

    def test_nueva_fuente_queda_habilitada(self, session):
        entries = [SourceEntry("Test", "https://test.com/rss", "test")]
        sync_sources_to_db(entries, session)
        session.commit()

        from sqlalchemy import select
        src = session.execute(select(Source)).scalars().first()
        assert src.enabled is True

    def test_sync_vacio_no_modifica_bd(self, session):
        sync_sources_to_db(self._entries(), session)
        session.commit()

        result = sync_sources_to_db([], session)
        session.commit()

        assert result.inserted == 0
        assert result.updated == 0


# ─── Tests: load_and_sync ────────────────────────────────────────────────────

class TestLoadAndSync:
    def test_integración_completa(self, valid_yaml, session):
        """Prueba el flujo completo: YAML → BD."""
        result = load_and_sync(valid_yaml, session)
        session.commit()

        assert isinstance(result, SyncResult)
        assert result.inserted == 3   # 2 argentina + 1 tecnologia

    def test_idempotente(self, valid_yaml, session):
        load_and_sync(valid_yaml, session)
        session.commit()

        result = load_and_sync(valid_yaml, session)
        session.commit()

        assert result.inserted == 0
        assert result.unchanged == 3

    def test_str_sync_result(self):
        r = SyncResult(inserted=2, updated=1, unchanged=5)
        assert "2" in str(r)
        assert "1" in str(r)
        assert "5" in str(r)
