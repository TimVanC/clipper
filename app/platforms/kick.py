from __future__ import annotations

import time

import httpx

from ..captions import same_category
from .base import LiveInfo

TOKEN_URL = "https://id.kick.com/oauth/token"
CHANNELS_URL = "https://api.kick.com/public/v1/channels"
CATEGORIES_URL = "https://api.kick.com/public/v2/categories"
LIVESTREAMS_V1_URL = "https://api.kick.com/public/v1/livestreams"  # deprecated, but the only one sorted by viewers
LIVESTREAMS_V2_URL = "https://api.kick.com/public/v2/livestreams"


class KickClient:
    """Official Kick public API, app access token (client credentials)."""

    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient):
        self.client_id, self.client_secret, self.http = client_id, client_secret, http
        self._token: str | None = None
        self._expires = 0.0
        self._category_ids: dict[str, int] = {}

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
            r = await self.http.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401 and attempt == 0:
                continue
            r.raise_for_status()
            return r.json().get("data", [])
        return []

    async def _channels(self, slugs: list[str]) -> list[dict]:
        return await self._get(CHANNELS_URL, [("slug", s) for s in slugs])

    async def category_id(self, name: str) -> int:
        """Kick names some games with a suffix ('Grand Theft Auto VI (GTA)'); that still matches."""
        key = name.strip().lower()
        if key not in self._category_ids:
            found = [c for c in await self._get(CATEGORIES_URL, [("name", name.strip())])
                     if same_category(str(c.get("name", "")), name)]
            if not found:
                raise ValueError(f"Kick has no category named {name!r}")
            self._category_ids[key] = int(found[0]["id"])
        return self._category_ids[key]

    async def top_streams(self, category: str, languages: list[str] | None = None) -> list[LiveInfo]:
        """Live streams in a category, most viewers first."""
        cid = await self.category_id(category)
        langs = [lang.lower() for lang in languages or []]
        try:
            params = [("category_id", str(cid)), ("sort", "viewer_count"), ("limit", "100")]
            if len(langs) == 1:
                params.append(("language", langs[0]))
            rows = [(s.get("slug", ""), s.get("stream_title", ""), s.get("language", ""), s)
                    for s in await self._get(LIVESTREAMS_V1_URL, params)]
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (404, 410):
                raise
            params = [("category_id", str(cid)), ("limit", "1000")] + [("language_code", lang) for lang in langs]
            rows = [((s.get("channel") or {}).get("slug", ""), s.get("title", ""), s.get("language_code", ""), s)
                    for s in await self._get(LIVESTREAMS_V2_URL, params)]
        out = []
        for slug, title, lang, s in rows:
            if not slug or (langs and str(lang).lower() not in langs):
                continue
            out.append(LiveInfo("kick", str(slug).lower(), title or "", str(s.get("started_at", "")),
                                f"https://kick.com/{str(slug).lower()}", int(s.get("viewer_count", 0) or 0),
                                str((s.get("category") or {}).get("name", "") or ""),
                                str((s.get("broadcaster_user") or {}).get("username", "") or "")))
        return sorted(out, key=lambda i: -i.viewers)

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
                                         int(st.get("viewer_count", 0) or 0),
                                         str((ch.get("category") or {}).get("name", "") or ""))
        return out
