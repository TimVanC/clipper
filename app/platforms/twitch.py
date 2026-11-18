from __future__ import annotations

import time

import httpx

from .base import LiveInfo

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"
GAMES_URL = "https://api.twitch.tv/helix/games"


class TwitchClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient):
        self.client_id, self.client_secret, self.http = client_id, client_secret, http
        self._token: str | None = None
        self._expires = 0.0
        self._game_ids: dict[str, str] = {}

    async def _get_token(self, force: bool = False) -> str:
        if self._token and not force and time.time() < self._expires - 60:
            return self._token
        r = await self.http.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "client_credentials"})
        r.raise_for_status()
        j = r.json()
        self._token = j["access_token"]
        self._expires = time.time() + float(j.get("expires_in", 3600))
        return self._token

    async def _get(self, url: str, params: list[tuple[str, str]]) -> list[dict]:
        for attempt in (0, 1):
            token = await self._get_token(force=attempt == 1)
            r = await self.http.get(url, params=params,
                                    headers={"Authorization": f"Bearer {token}", "Client-Id": self.client_id})
            if r.status_code == 401 and attempt == 0:
                continue
            r.raise_for_status()
            return r.json().get("data", [])
        return []

    async def _streams(self, logins: list[str]) -> list[dict]:
        return await self._get(STREAMS_URL, [("user_login", h) for h in logins] + [("first", "100")])

    @staticmethod
    def _info(s: dict) -> LiveInfo:
        login = s["user_login"].lower()
        return LiveInfo("twitch", login, s.get("title", ""), str(s.get("id", "")), f"https://www.twitch.tv/{login}",
                        int(s.get("viewer_count", 0)), s.get("game_name", "") or "", s.get("user_name", "") or "")

    async def category_id(self, name: str) -> str:
        key = name.strip().lower()
        if key not in self._game_ids:
            games = await self._get(GAMES_URL, [("name", name.strip())])
            if not games:
                raise ValueError(f"Twitch has no category named {name!r}")
            self._game_ids[key] = str(games[0]["id"])
        return self._game_ids[key]

    async def top_streams(self, category: str, languages: list[str] | None = None) -> list[LiveInfo]:
        """Live streams in a category, most viewers first (Helix returns them in that order)."""
        params = [("game_id", await self.category_id(category)), ("first", "100"), ("type", "live")]
        params += [("language", lang) for lang in (languages or [])]
        return [self._info(s) for s in await self._get(STREAMS_URL, params) if s.get("type", "live") == "live"]

    async def check_many(self, handles: list[str]) -> dict[str, LiveInfo | None]:
        out: dict[str, LiveInfo | None] = {h.lower(): None for h in handles}
        for i in range(0, len(handles), 100):
            batch = [h.lower() for h in handles[i:i + 100]]
            for s in await self._streams(batch):
                if s.get("type", "live") != "live":
                    continue
                info = self._info(s)
                out[info.handle] = info
        return out
