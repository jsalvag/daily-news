"""
Generación de audio TTS para briefings diarios.

Soporta cuatro proveedores:
  - edge-tts   : GRATIS, sin cuenta ni API key. Voces neurales de Microsoft Edge.
                 Calidad excelente. Recomendado para uso cotidiano.
  - OpenAI TTS : voces neurales de pago (~$0.015-0.03/1K chars).
  - ElevenLabs : ultra-realistas, requiere plan Starter ($5/mes).
  - Google TTS : 4M chars/mes gratis (Standard) pero requiere billing habilitado.

Uso típico (edge-tts, sin costo):
    from app.tts.generate import generate_audio_for_briefing
    path = generate_audio_for_briefing(
        text="Buenos días. Hoy en las noticias...",
        output_path=Path("data/audio/briefing-2024-01-15.mp3"),
        provider="edge",
        api_key="",          # no se usa
        voice="es-MX-DaliaNeural",
    )
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Timeout generoso para textos largos: TTS puede tardar varios segundos
_HTTP_TIMEOUT = 90.0


# ─── Proveedores ──────────────────────────────────────────────────────────────

def _generate_openai(
    text: str,
    api_key: str,
    voice: str = "nova",
    model: str = "tts-1-hd",
) -> bytes:
    """
    Genera audio MP3 usando la API de OpenAI TTS.

    Voces disponibles: alloy, echo, fable, onyx, nova, shimmer.
    nova y onyx son las más naturales para español.
    """
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        response = client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": text,
                "voice": voice,
                "response_format": "mp3",
            },
        )
        response.raise_for_status()
        return response.content


def _generate_elevenlabs(
    text: str,
    api_key: str,
    voice_id: str,
    model_id: str = "eleven_multilingual_v2",
) -> bytes:
    """
    Genera audio MP3 usando la API de ElevenLabs.

    eleven_multilingual_v2 soporta español nativo con alta calidad.
    El voice_id se obtiene desde https://elevenlabs.io/voice-lab
    o vía GET https://api.elevenlabs.io/v1/voices
    """
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        response = client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": model_id,
                "voice_settings": {
                    "stability": 0.50,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": True,
                },
            },
        )
        response.raise_for_status()
        return response.content


def _generate_edge(text: str, voice: str = "es-MX-DaliaNeural") -> bytes:
    """
    Genera audio MP3 usando edge-tts (voces neurales de Microsoft Edge).

    Completamente gratuito. Sin cuenta ni API key.
    Voces españolas recomendadas:
      es-MX-DaliaNeural  (femenina, mexicana)
      es-MX-JorgeNeural  (masculina, mexicano)
      es-ES-ElviraNeural (femenina, España)
      es-ES-AlvaroNeural (masculina, España)
    """
    import asyncio
    import tempfile
    import edge_tts

    async def _synth() -> bytes:
        communicate = edge_tts.Communicate(text, voice)
        # Escribe a un archivo temporal para obtener bytes MP3
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        await communicate.save(tmp_path)
        with open(tmp_path, "rb") as f:
            data = f.read()
        import os
        os.unlink(tmp_path)
        return data

    # Ejecutar el coroutine de forma síncrona (compatible con threads de FastAPI)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _synth())
                return future.result()
        else:
            return loop.run_until_complete(_synth())
    except RuntimeError:
        return asyncio.run(_synth())


def _generate_gtts(
    text: str,
    lang: str = "es",
    tld: str = "com.mx",
) -> bytes:
    """
    Genera audio MP3 usando Google Translate TTS (gTTS).

    Completamente gratuito. Sin cuenta ni API key.
    Es el mismo servicio que usa Home Assistant con "google_translate" TTS.

    Opciones de idioma + acento para español:
      lang="es", tld="com.mx"  → español mexicano
      lang="es", tld="es"      → español de España
      lang="es", tld="com"     → español neutro (EE.UU.)
      lang="es", tld="com.ar"  → español argentino
    """
    import io
    from gtts import gTTS

    tts = gTTS(text=text, lang=lang, tld=tld, slow=False)
    mp3_fp = io.BytesIO()
    tts.write_to_fp(mp3_fp)
    return mp3_fp.getvalue()


def _generate_google_cloud(
    text: str,
    api_key: str,
    voice_name: str = "es-MX-Standard-A",
    language_code: str = "es-MX",
) -> bytes:
    """
    Genera audio MP3 usando Google Cloud Text-to-Speech REST API.

    Plan gratuito: 4M chars/mes con voces Standard, 1M chars/mes con WaveNet/Neural2.
    Requiere API key de Google Cloud con la API "Cloud Text-to-Speech" habilitada.

    Voces Standard recomendadas para español:
      es-MX-Standard-A (femenina), es-MX-Standard-B (masculina)
      es-ES-Standard-A (femenina), es-ES-Standard-B (masculina)
    Voces de mayor calidad (dentro del tier gratuito):
      es-MX-Wavenet-A, es-US-Neural2-A
    """
    import base64

    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        response = client.post(
            "https://texttospeech.googleapis.com/v1/text:synthesize",
            params={"key": api_key},
            headers={"Content-Type": "application/json"},
            json={
                "input": {"text": text},
                "voice": {
                    "languageCode": language_code,
                    "name": voice_name,
                },
                "audioConfig": {"audioEncoding": "MP3"},
            },
        )
        response.raise_for_status()
        return base64.b64decode(response.json()["audioContent"])


# ─── Función principal ────────────────────────────────────────────────────────

def generate_audio_for_briefing(
    text: str,
    output_path: Path,
    provider: str,
    api_key: str,
    voice: str,
    openai_model: str = "tts-1-hd",
    elevenlabs_model: str = "eleven_multilingual_v2",
    google_language_code: str = "es-MX",
    gtts_lang: str = "es",
    gtts_tld: str = "com.mx",
) -> Path:
    """
    Genera el archivo MP3 de un briefing y lo guarda en disco.

    Args:
        text:                 Texto a convertir a voz.
        output_path:          Ruta donde guardar el archivo MP3.
        provider:             "gtts" | "edge" | "openai" | "elevenlabs" | "google"
        api_key:              API key del proveedor (no se usa para gtts/edge).
        voice:                Nombre de voz según el proveedor.
        openai_model:         "tts-1" o "tts-1-hd".
        elevenlabs_model:     Modelo de ElevenLabs.
        google_language_code: Código de idioma para Google Cloud (ej: "es-MX").
        gtts_lang:            Código de idioma para gTTS (ej: "es").
        gtts_tld:             TLD para acento regional en gTTS (ej: "com.mx").

    Returns:
        Path del archivo generado.

    Raises:
        ValueError:            Si el proveedor no está soportado.
        httpx.HTTPStatusError: Si la API rechaza la solicitud.
        httpx.TimeoutException: Si la API no responde a tiempo.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if provider == "gtts":
        audio_bytes = _generate_gtts(text=text, lang=gtts_lang, tld=gtts_tld)
    elif provider == "edge":
        audio_bytes = _generate_edge(text=text, voice=voice)
    elif provider == "openai":
        audio_bytes = _generate_openai(
            text=text, api_key=api_key, voice=voice, model=openai_model,
        )
    elif provider == "elevenlabs":
        audio_bytes = _generate_elevenlabs(
            text=text, api_key=api_key, voice_id=voice, model_id=elevenlabs_model,
        )
    elif provider == "google":
        audio_bytes = _generate_google_cloud(
            text=text, api_key=api_key, voice_name=voice,
            language_code=google_language_code,
        )
    else:
        raise ValueError(
            f"Proveedor TTS no soportado: {provider!r}. "
            "Usar 'gtts', 'edge', 'openai', 'elevenlabs' o 'google'."
        )

    output_path.write_bytes(audio_bytes)
    logger.info(
        "Audio TTS generado: %s (proveedor=%s, voz=%s, %d bytes)",
        output_path.name, provider, voice, len(audio_bytes),
    )
    return output_path
