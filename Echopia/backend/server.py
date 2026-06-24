"""Echopia WebSocket server (Phase 0).

Runs one EchopiaEngine, broadcasts a per-tick payload to all connected clients,
and applies any control messages clients send. Also serves the static front-end
files (the throwaway test page now; the Three.js game later) over HTTP so the
whole thing is one command to launch.

    python -m backend.server          # from the Echopia/ folder
    # then open http://localhost:8765/
"""

from __future__ import annotations

import asyncio
import http
import json
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from backend.engine import EchopiaEngine

HOST = "localhost"
PORT = 8765
STATIC_DIR = Path(__file__).resolve().parents[1]  # Echopia/

engine = EchopiaEngine(tick_hz=20.0)
clients: set = set()

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


async def broadcaster() -> None:
    """Advance the engine on a fixed cadence and fan out to all clients."""
    period = 1.0 / engine.tick_hz
    loop = asyncio.get_running_loop()
    next_t = loop.time()
    while True:
        payload = json.dumps(engine.tick())
        if clients:
            await asyncio.gather(
                *(c.send(payload) for c in list(clients)),
                return_exceptions=True,
            )
        next_t += period
        await asyncio.sleep(max(0.0, next_t - loop.time()))


async def handler(ws) -> None:
    clients.add(ws)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                engine.apply_control(msg)
    finally:
        clients.discard(ws)


async def serve_static(connection, request):
    """websockets process_request hook: serve static files for plain HTTP GETs.

    Returning None lets a request fall through to the WebSocket handshake.
    """
    path = request.path.split("?", 1)[0]
    if path in ("", "/"):
        path = "/index.html"
    # Allow the eventual game entry point too.
    # WebSocket upgrades must fall through to the handshake.
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    target = (STATIC_DIR / path.lstrip("/")).resolve()
    if STATIC_DIR not in target.parents or not target.is_file():
        return connection.respond(http.HTTPStatus.NOT_FOUND, "Not found\n")
    body = target.read_bytes()
    content_type = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    return Response(http.HTTPStatus.OK.value, "OK", headers, body)


async def main() -> None:
    async with serve(handler, HOST, PORT, process_request=serve_static):
        print(f"Echopia server: http://{HOST}:{PORT}/  (WS on same port)")
        print(f"  engine: fs={engine.fs} Hz, tick={engine.tick_hz} Hz, n={engine.n_samples}")
        await broadcaster()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
