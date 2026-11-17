"""Manual mode (POSTER_PROVIDER=none): nothing is posted. Approve marks the clip "approved" and the GUI
offers the video as a download so you can upload it yourself. Free, no API key."""
from __future__ import annotations

from pathlib import Path

from .base import Poster, PosterError


class ManualPoster(Poster):
    name, label, env_key, manual = "none", "Manual (download)", "", True

    def __init__(self, *_a, **_kw):
        self.key, self.http, self.base = "", None, ""

    async def list_accounts(self) -> list[dict]:
        return []

    async def publish(self, path: Path, caption: str, account_ids: list[str], external_id: str | None = None) -> dict:
        raise PosterError("manual mode: download the clip and post it yourself")
