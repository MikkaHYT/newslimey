"""The dashboard HTTP server.

Runs beside the bot in the same event loop, so every panel reads live Discord
state and every action goes through the bot's own client. One process, one
connection to Discord.

``DASHBOARD_PASSWORD`` unlocks it. If it is missing a random one is generated
for the run and logged once, so the dashboard never comes up unauthenticated.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .api import setup_routes
from .auth import DashboardAuth, generate_password, make_auth_middleware
from .live import LiveHub

logger = logging.getLogger("slimey.dashboard")

STATIC_DIR = Path(__file__).parent / "static"

#: Applied to every response. The dashboard loads its own scripts only; images
#: and media may come from Discord's CDN.
CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https://cdn.discordapp.com https://media.discordapp.net "
    "https://media.tenor.com https://c.tenor.com https://media.giphy.com https://i.giphy.com; "
    "media-src 'self' https://cdn.discordapp.com https://media.discordapp.net; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self' ws: wss:; "
    "font-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    if isinstance(response, web.StreamResponse) and response.prepared:
        # Streamed media was already sent; headers cannot be added now.
        return response
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", CSP)
    return response


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


async def favicon(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "favicon.svg")


async def status(request: web.Request) -> web.Response:
    """Liveness probe for the container healthcheck; no secrets in here."""
    bot = request.app.get("bot")
    try:
        latency = round(float(getattr(bot, "latency", 0.0)) * 1000)
    except (TypeError, ValueError):
        latency = None
    return web.json_response(
        {
            "ok": True,
            "uptime": int(time.time() - float(request.app.get("started_at") or time.time())),
            "guilds": len(getattr(bot, "guilds", []) or []),
            "latency_ms": latency,
        }
    )


def create_app(bot: Any, password: str) -> web.Application:
    app = web.Application(
        client_max_size=64 * 1024 * 1024,
        middlewares=[security_headers],
    )
    app["bot"] = bot
    app["started_at"] = time.time()
    app["auth"] = DashboardAuth(password)
    app["hub"] = LiveHub(bot)
    app.middlewares.append(make_auth_middleware(app["auth"]))

    setup_routes(app)
    app.router.add_get("/", index)
    app.router.add_get("/favicon.svg", favicon)
    app.router.add_get("/status", status)
    app.router.add_get("/ws", app["hub"].websocket)
    app.router.add_static("/static/", STATIC_DIR, show_index=False, follow_symlinks=False)
    return app


def _resolve_password() -> tuple[str, bool]:
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if password:
        return password, False
    generated = generate_password()
    return generated, True


async def start_dashboard(bot: Any) -> None:
    """Start the dashboard; failures are logged, never fatal to the bot."""
    if not STATIC_DIR.exists():
        raise RuntimeError(f"Dashboard assets are missing from {STATIC_DIR}")

    host = os.getenv("DASHBOARD_HOST", "0.0.0.0").strip() or "0.0.0.0"
    # ADMIN_API_PORT is what the Dockerfile healthcheck already uses.
    raw_port = os.getenv("DASHBOARD_PORT") or os.getenv("ADMIN_API_PORT") or "25565"
    try:
        port = int(raw_port)
    except ValueError:
        port = 25565

    password, generated = _resolve_password()
    app = create_app(bot, password)
    app["hub"].install()

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    bot.web_runner = runner
    bot.dashboard_port = port
    logger.info("Web dashboard listening on http://%s:%s", host, port)
    if generated:
        logger.warning(
            "DASHBOARD_PASSWORD is not set, so this run uses a generated password: %s",
            password,
        )
        logger.warning(
            "Set DASHBOARD_PASSWORD in the environment to keep a password of your own."
        )


async def stop_dashboard(bot: Any) -> None:
    runner = getattr(bot, "web_runner", None)
    if runner is None:
        return
    bot.web_runner = None
    try:
        await runner.cleanup()
        logger.info("Web dashboard stopped.")
    except Exception:
        logger.exception("Could not stop the web dashboard cleanly")
