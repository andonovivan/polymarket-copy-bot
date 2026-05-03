"""Threaded HTTP server: serves static files + delegates /api/* to api handlers."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import structlog

from polymarket_bot.dashboard import api
from polymarket_bot.config import BotConfig

logger = structlog.get_logger()

STATIC_DIR = Path(__file__).parent / "static"

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "polymarket-bot/0.2"
    config: BotConfig | None = None

    def log_message(self, fmt: str, *args) -> None:  # quieter than default
        return

    def _send(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _serve_static(self, rel: str) -> None:
        if rel in ("", "/"):
            rel = "index.html"
        rel = rel.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send(403, b"forbidden")
            return
        if not target.is_file():
            # SPA fallback so client-side routes still load index.html.
            target = STATIC_DIR / "index.html"
            if not target.is_file():
                self._send(404, b"not found")
                return
        ext = target.suffix.lower()
        ct = _MIME.get(ext, "application/octet-stream")
        self._send(200, target.read_bytes(), ct)

    def do_GET(self) -> None:  # noqa: N802 — stdlib API
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                status, payload = api.dispatch_get(path, qs, self.config)
                self._send_json(status, payload)
                return
            self._serve_static(path)
        except Exception as exc:
            logger.error("dashboard_error", path=path, error=str(exc))
            self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b""
        try:
            payload_in = json.loads(body) if body else {}
        except Exception:
            payload_in = {}
        try:
            if parsed.path.startswith("/api/"):
                status, payload = api.dispatch_post(parsed.path, payload_in, self.config)
                self._send_json(status, payload)
                return
            self._send(404, b"not found")
        except Exception as exc:
            logger.error("dashboard_error", path=parsed.path, error=str(exc))
            self._send_json(500, {"error": str(exc)})


def start_dashboard(config: BotConfig) -> threading.Thread:
    """Start the dashboard in a background thread. Returns the thread."""
    _Handler.config = config

    def _run() -> None:
        server = ThreadingHTTPServer(("0.0.0.0", config.dashboard_port), _Handler)
        logger.info("dashboard_listening", port=config.dashboard_port)
        try:
            server.serve_forever()
        except Exception as exc:
            logger.error("dashboard_crashed", error=str(exc))

    t = threading.Thread(target=_run, name="dashboard", daemon=True)
    t.start()
    return t
