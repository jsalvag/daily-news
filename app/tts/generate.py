"""
Generación de audio TTS para briefings diarios.

Soporta dos proveedores:
  - OpenAI TTS   : voces neurales (nova, onyx, alloy, echo, fable, shimmer)
                   Modelos: tts-1 (rápido) / tts-1-hd (máxima calidad)
  - ElevenLabs   : voces ultra-realistas, ideal para contenido editorial.
                   Modelo: eleven_multilingual_v2 (soporta español nativo)

Uso típico:
    from app.tts.generate import generate_audio_for_briefing
    path = generate_audio_for_briefing(
        text="Buenos días. Hoy en las noticias...",
        output_path=Path("data/audio/briefing-2024-01-15.mp3"),
        provider="openai",
        api_key="sk-...",
        voice="nova",
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


# ─── Función principal ────────────────────────────────────────────────────────

def generate_audio_for_briefing(
    text: str,
    output_path: Path,
    provider: str,
    api_key: str,
    voice: str,
    openai_model: str = "tts-1-hd",
    elevenlabs_model: str = "eleven_multilingual_v2",
) -> Path:
    """
    Genera el archivo MP3 de un briefing y lo guarda en disco.

    Args:
        text:             Texto a convertir a voz.
        output_path:      Ruta donde guardar el archivo MP3.
        provider:         "openai" | "elevenlabs"
        api_key:          API key del proveedor.
        voice:            Nombre de voz (OpenAI) o voice_id (ElevenLabs).
        openai_model:     "tts-1" (rápido) o "tts-1-hd" (máxima calidad).
        elevenlabs_model: Modelo de ElevenLabs (default: eleven_multilingual_v2).

    Returns:
        Path del archivo generado.

    Raises:
        ValueError:          Si el proveedor no está soportado.
        httpx.HTTPStatusError: Si la API rechaza la solicitud (key inválida, etc).
        httpx.TimeoutException: Si la API no responde a tiempo.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if provider == "openai":
        audio_bytes = _generate_openai(
            text=text,
            api_key=api_key,
            voice=voice,
            model=openai_model,
        )
    elif provider == "elevenlabs":
        audio_bytes = _generate_elevenlabs(
            text=text,
            api_key=api_key,
            voice_id=voice,
            model_id=elevenlabs_model,
        )
    else:
        raise ValueError(f"Proveedor TTS no soportado: {provider!r}. Usar 'openai' o 'elevenlabs'.")

    output_path.write_bytes(audio_bytes)
    logger.info(
        "Audio TTS generado: %s (proveedor=%s, voz=%s, %d bytes)",
        output_path.name, provider, voice, len(audio_bytes),
    )
    return output_path
