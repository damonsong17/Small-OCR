"""A dashboard server built from the standard library only.

No Flask, no Node, no CDN — the offline bundle already has enough moving parts.
``http.server`` serves a handful of JSON endpoints and a static folder; the
front end is plain HTML, CSS and JavaScript with the world map bundled as a
file. Nothing here reaches the network.

    python dashboard.py --data data/treasury
"""
from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from . import book as book_module
from . import overlays
from .config import Settings
from .metrics import REGISTRY, catalogue, compute
from .metrics.clients_map import country_context

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class App:
    """Holds the loaded Book and rebuilds it on demand."""

    def __init__(self, settings: Settings, quotes_db: str = ""):
        self.settings = settings
        self.quotes_db = quotes_db or settings.quotes_db
        self.lock = threading.Lock()
        self.book: Optional[book_module.Book] = None
        self.error: str = ""
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        with self.lock:
            self.cache.clear()
            try:
                self.book = book_module.load_sources(
                    self.settings.data_dir, self.quotes_db,
                    **{k: v for k, v in (("as_of", self.settings.as_of),) if v})
                self.settings = self.book.settings
                self.error = ""
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"

    def panel(self, metric_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        key = metric_id + "?" + urllib.parse.urlencode(sorted(params.items()))
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            if self.book is None:
                return {"id": metric_id, "error": self.error or "no data loaded"}
            panel = compute(self.book, metric_id, **params)
            self.cache[key] = panel
            return panel

    def overview(self) -> Dict[str, Any]:
        """Headline numbers from every available metric, on one screen."""
        if self.book is None:
            return {"error": self.error or "no data loaded", "cards": []}
        cards = []
        for spec in sorted(REGISTRY.values(), key=lambda m: (m.order, m.title)):
            if spec.missing(self.book) or spec.group == "Data":
                continue
            panel = self.panel(spec.id, {})
            if panel.get("error"):
                continue
            cards.append({
                "id": spec.id, "title": spec.title, "group": spec.group,
                "hero": panel.get("hero"),
                "kpis": (panel.get("kpis") or [])[:4],
                "has_map": bool(panel.get("map")),
            })
        return {"cards": cards}

    def snapshot(self) -> Dict[str, Any]:
        from .store import open_store
        if self.book is None:
            return {"error": self.error or "no data loaded"}
        panels = {}
        for spec in REGISTRY.values():
            if spec.missing(self.book) or spec.group == "Data":
                continue
            panels[spec.id] = self.panel(spec.id, {})
        store = open_store(self.settings)
        if store is None:
            return {"error": "could not open the snapshot store"}
        try:
            written = store.snapshot(self.book, panels)
        finally:
            store.close()
        return {"as_of": self.book.as_of, "written": written}

    def history(self, metric_id: str, key: str) -> Dict[str, Any]:
        from .store import open_store
        store = open_store(self.settings)
        if store is None:
            return {"points": []}
        try:
            points = store.history(metric_id, key)
        finally:
            store.close()
        return {"metric": metric_id, "key": key,
                "points": [{"as_of": d, "value": v} for d, v in points]}

    def state(self) -> Dict[str, Any]:
        if self.book is None:
            return {"ok": False, "error": self.error}
        return {
            "ok": True,
            "entity": self.settings.entity,
            "as_of": self.book.as_of,
            "base_currency": self.settings.base_currency,
            "data_dir": os.path.abspath(self.settings.data_dir),
            "settings_file": self.settings.loaded_from,
            "counts": self.book.counts(),
            "warnings": self.book.warnings,
            "metrics": catalogue(self.book),
            "currencies": self.book.currencies(),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "TreasuryDashboard/1.0"
    app: App = None            # type: ignore[assignment]

    # -- plumbing -------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("TREASURY_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str,
              cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        # The page is entirely self-contained; forbid anything else outright.
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         "connect-src 'self'; base-uri 'none'; form-action 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _static(self, relative: str) -> None:
        safe = posixpath.normpath("/" + relative).lstrip("/")
        path = os.path.join(STATIC_DIR, safe)
        if not os.path.abspath(path).startswith(os.path.abspath(STATIC_DIR)) \
                or not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith(("javascript", "json")):
            content_type += "; charset=utf-8"
        with open(path, "rb") as fh:
            body = fh.read()
        self._send(200, body, content_type, cache="no-cache")

    # -- routes ---------------------------------------------------------------
    def do_GET(self) -> None:          # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            self._route(route, params)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(limit=6)}, 500)

    def do_HEAD(self) -> None:         # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:         # noqa: N802
        route = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if route == "/api/reload":
            self.app.reload()
            self._json(self.app.state())
        elif route == "/api/snapshot":
            self._json(self.app.snapshot())
        else:
            self._json({"error": "not found"}, 404)

    def _route(self, route: str, params: Dict[str, str]) -> None:
        if route == "/":
            self._static("index.html")
        elif route == "/api/state":
            self._json(self.app.state())
        elif route == "/api/overview":
            self._json(self.app.overview())
        elif route.startswith("/api/metric/"):
            self._json(self.app.panel(route[len("/api/metric/"):], params))
        elif route.startswith("/api/country/"):
            iso2 = route[len("/api/country/"):].upper()[:3]
            if self.app.book is None:
                self._json({"error": self.app.error or "no data loaded"}, 503)
                return
            context = country_context(self.app.book, iso2)
            self._json({**{k: v for k, v in context.items() if k != "clients"},
                        "sections": overlays.build(self.app.book, iso2, context)})
        elif route.startswith("/api/history/"):
            parts = route[len("/api/history/"):].split("/")
            self._json(self.app.history(parts[0], parts[1] if len(parts) > 1 else "hero"))
        elif route == "/api/health":
            self._json({"ok": self.app.book is not None, "error": self.app.error})
        else:
            self._static(route.lstrip("/"))


def serve(settings: Settings, quotes_db: str = "", open_browser: bool = False) -> None:
    app = App(settings, quotes_db)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    url = f"http://{settings.host}:{settings.port}/"

    print(f"Treasury dashboard  {url}")
    print(f"  data      {os.path.abspath(settings.data_dir)}")
    print(f"  as of     {app.book.as_of if app.book else '—'}")
    if app.book:
        counts = ", ".join(f"{n}={c}" for n, c in sorted(app.book.counts().items()))
        print(f"  loaded    {counts or 'nothing — check the data folder'}")
        for warning in app.book.warnings:
            print(f"  warning   {warning}")
    if app.error:
        print(f"  ERROR     {app.error.splitlines()[0]}")
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        print("  NOTE      bound beyond localhost and there is no authentication — "
              "only do this on a trusted internal network")
    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print("  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
