from __future__ import annotations

import time

import httpx

from .base import LiveInfo

TOKEN_URL = "https://id.kick.com/oauth/token"
CHANNELS_URL = "https://api.kick.com/public/v1/channels"


class KickClient:
    """Official Kick public API, app access token (client credentials)."""

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

    async def _channels(self, slugs: list[str]) -> list[dict]:
        for attempt in (0, 1):
            token = await self._get_token(force=attempt == 1)
            r = await self.http.get(CHANNELS_URL, params=[("slug", s) for s in slugs],
                                    headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401 and attempt == 0:
                continue
            r.raise_for_status()
            return r.json().get("data", [])
        return []

    async def check_many(self, handles: list[str]) -> dict[str, LiveInfo | None]:
        out: dict[str, LiveInfo | None] = {h.lower(): None for h in handles}
        for i in range(0, len(handles), 50):
            batch = [h.lower() for h in handles[i:i + 50]]
            for ch in await self._channels(batch):
                slug = str(ch.get("slug", "")).lower()
                st = ch.get("stream") or {}
                if slug and st.get("is_live"):
                    out[slug] = LiveInfo("kick", slug, ch.get("stream_title", "") or "",
                                         str(st.get("start_time", "")), f"https://kick.com/{slug}",
                                         int(st.get("viewer_count", 0) or 0))
        return out
