"""
Bus de eventos SSE (Server-Sent Events) para el pipeline de noticias.

Los jobs corren en threads (APScheduler / FastAPI BackgroundTasks).
Usamos un modelo publish/subscribe con queue.Queue por cliente SSE.

Uso:
    # Desde un job (thread):
    from app.events import publish
    publish("job_done", {"job": "fetch", "inserted": 42})

    # Desde un endpoint FastAPI (async):
    from app.events import event_stream
    return StreamingResponse(event_stream(), media_type="text/event-stream")
"""
from __future__ import annotations

import json
import queue
import threading
from typing import AsyncIterator

_lock = threading.Lock()
_clients: dict[int, queue.Queue] = {}
_next_id = 0


def _new_client() -> tuple[int, queue.Queue]:
    global _next_id
    with _lock:
        cid = _next_id
        _next_id += 1
        q: queue.Queue = queue.Queue(maxsize=500)
        _clients[cid] = q
        return cid, q


def _remove_client(cid: int) -> None:
    with _lock:
        _clients.pop(cid, None)


def publish(event_type: str, data: dict) -> None:
    """
    Publica un evento a todos los clientes SSE conectados.

    Seguro para llamar desde cualquier thread. Los clientes lentos
    o desconectados (cola llena) se eliminan automáticamente.
    """
    payload = json.dumps(data, ensure_ascii=False)
    msg = {"type": event_type, "data": payload}
    with _lock:
        dead: list[int] = []
        for cid, q in _clients.items():
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(cid)
        for cid in dead:
            _clients.pop(cid, None)


async def event_stream() -> AsyncIterator[str]:
    """
    Generador asíncrono SSE. Cada llamada crea un cliente independiente.

    - Bloquea en q.get con timeout=1 s para no saturar el event loop.
    - Emite un comentario keep-alive cada ~15 s para mantener la conexión.
    - Limpia el cliente al cerrar (desconexión del navegador).
    """
    import asyncio

    loop = asyncio.get_event_loop()
    cid, q = _new_client()

    def _try_get() -> dict | None:
        try:
            return q.get(timeout=1.0)
        except queue.Empty:
            return None

    try:
        ticks_since_keepalive = 0
        while True:
            event = await loop.run_in_executor(None, _try_get)
            if event is not None:
                ticks_since_keepalive = 0
                yield f"event: {event['type']}\ndata: {event['data']}\n\n"
            else:
                ticks_since_keepalive += 1
                if ticks_since_keepalive >= 15:
                    ticks_since_keepalive = 0
                    yield ": keep-alive\n\n"
    except (GeneratorExit, asyncio.CancelledError):
        pass
    finally:
        _remove_client(cid)
