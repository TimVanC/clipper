"""Entry point: `python -m app.main` (or uvicorn app.main:app)."""
from __future__ import annotations

import logging
import os

import httpx

from .config import Settings
from .db import Database
from .notifier import DiscordNotifier
from .pipeline import Pipeline
from .platforms.kick import KickClient
from .platforms.twitch import TwitchClient
from .platforms.youtube import YouTubeClient
from .posters import make_poster
from .recorder import contain_children
from .web import create_app


def build_pipeline(settings: Settings, http: httpx.AsyncClient | None = None, **kw) -> Pipeline:
    http = http or httpx.AsyncClient(timeout=30, follow_redirects=True)
    db = Database(settings.data_dir / "clipper.db")
    clients = {}
    if settings.twitch_client_id and settings.twitch_client_secret:
        clients["twitch"] = TwitchClient(settings.twitch_client_id, settings.twitch_client_secret, http)
    if settings.kick_client_id and settings.kick_client_secret:
        clients["kick"] = KickClient(settings.kick_client_id, settings.kick_client_secret, http)
    if settings.youtube_api_key:
        clients["youtube"] = YouTubeClient(settings.youtube_api_key, http)
    notifier = DiscordNotifier(settings.discord_webhook_url, http, settings.public_base_url,
                               settings.discord_max_upload_mb)
    poster = make_poster(settings, http)
    return Pipeline(settings, db, clients, notifier, poster, **kw)


def create() -> "FastAPI":  # noqa: F821
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    contain_children()  # recorders die with the app, however it ends
    return create_app(build_pipeline(settings))


_app = None


def __getattr__(name):  # lazy `app` so `import app.main` in tests doesn't build a pipeline; uvicorn app.main:app works
    global _app
    if name == "app":
        if _app is None:
            _app = create()
        return _app
    raise AttributeError(name)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
