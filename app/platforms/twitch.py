from __future__ import annotations

import time

import httpx

from .base import LiveInfo

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"


class TwitchClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient):
        self.client_id, self.client_secret, self.http = client_id, client_secret, http
        self._token: str | None = None
        self._expires = 0.0

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

    async def _streams(self, logins: list[str]) -> list[dict]:
        for attempt in (0, 1):
            token = await self._get_token(force=attempt == 1)
            params = [("user_login", h) for h in logins] + [("first", "100")]
            r = await self.http.get(STREAMS_URL, params=params,
                                    headers={"Authorization": f"Bearer {token}", "Client-Id": self.client_id})
            if r.status_code == 401 and attempt == 0:
                continue
            r.raise_for_status()
            return r.json().get("data", [])
        return []

    async def check_many(self, handles: list[str]) -> dict[str, LiveInfo | None]:
        out: dict[str, LiveInfo | None] = {h.lower(): None for h in handles}
        for i in range(0, len(handles), 100):
            batch = [h.lower() for h in handles[i:i + 100]]
            for s in await self._streams(batch):
                if s.get("type", "live") != "live":
                    continue
                login = s["user_login"].lower()
                out[login] = LiveInfo("twitch", login, s.get("title", ""), str(s.get("id", "")),
                                      f"https://www.twitch.tv/{login}", int(s.get("viewer_count", 0)))
        return out
