"""The Poster interface every posting provider implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import httpx


class PosterError(RuntimeError):
    """Any provider failure; the message is shown to the user."""


class Poster(ABC):
    name = ""       # POSTER_PROVIDER value
    label = ""      # human name for the GUI / doctor
    env_key = ""    # env var holding the API key
    manual = False  # True = nothing is posted, the user downloads the clip

    def __init__(self, api_key: str, http: httpx.AsyncClient, base_url: str):
        self.key, self.http, self.base = api_key, http, base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    @abstractmethod
    async def list_accounts(self) -> list[dict]:
        """Connected accounts as [{"id", "platform", "username", "status"}]; `id` is what publish() takes."""

    @abstractmethod
    async def publish(self, path: Path, caption: str, account_ids: list[str],
                      external_id: str | None = None) -> dict:
        """Upload the clip and post it to the given accounts. Returns at least {"post_id": str}."""

    def info(self) -> dict:
        return {"provider": self.name, "label": self.label, "manual": self.manual, "enabled": self.enabled,
                "env_key": self.env_key}
