"""The bot's web dashboard: aiohttp server, JSON API and static front end."""

from .server import start_dashboard, stop_dashboard

__all__ = ["start_dashboard", "stop_dashboard"]
