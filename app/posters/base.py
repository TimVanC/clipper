"""The Poster interface every posting provider implements."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

import httpx


# turned down because a daily limit was hit (TikTok's spam limit, a YouTube channel's daily limit, the
# provider's own cap): worth trying again later
LIMIT_CODES = {"youtube_channel_daily_limit", "account_restricted", "daily_cap_reached"}
LIMIT_WORDS = re.compile(r"too many posts|daily (posting )?(cap|limit)|number of videos it may upload|"
                         r"\b429\b|rate limit", re.I)


def is_limit(text: str, code: str = "") -> bool:
    return code in LIMIT_CODES or bool(LIMIT_WORDS.search(text or ""))


def first_sentence(text: str, limit: int = 200) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text.strip())
    return (m.group(1) if m else text.strip())[:limit]


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

    def checks(self, post_id: str) -> bool:
        """Can check() tell how this upload ended? If not, an accepted upload counts as live."""
        return False

    async def check(self, post_id: str) -> dict:
        """How a single-account upload ended: {"state": processing | live | failed | limited, "url", "error"}."""
        return {"state": "live", "url": "", "error": ""}

    def info(self) -> dict:
        return {"provider": self.name, "label": self.label, "manual": self.manual, "enabled": self.enabled,
                "env_key": self.env_key}
