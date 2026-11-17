from __future__ import annotations

import logging

import httpx

from .base import LiveInfo

log = logging.getLogger("clipper.youtube")
BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeClient:
    """Quota-friendly live detection (about 2 units/poll per channel, not search.list's 100):
    look at the channel's uploads playlist (live broadcasts appear there) and ask videos.list which
    of the newest items is currently live. `handle` may be a channel id (UC...) or an @handle."""

    def __init__(self, api_key: str, http: httpx.AsyncClient):
        self.key, self.http = api_key, http
        self._channel_ids: dict[str, str] = {}

    async def _get(self, path: str, **params) -> dict:
        r = await self.http.get(f"{BASE}/{path}", params={**params, "key": self.key})
        r.raise_for_status()
        return r.json()

    async def channel_id(self, handle: str) -> str:
        if handle.startswith("UC") and len(handle) >= 20:
            return handle
        if handle in self._channel_ids:
            return self._channel_ids[handle]
        j = await self._get("channels", part="id", forHandle="@" + handle.lstrip("@"))
        items = j.get("items", [])
        if not items:
            raise LookupError(f"youtube channel not found: {handle}")
        self._channel_ids[handle] = items[0]["id"]
        return items[0]["id"]

    async def _check_one(self, handle: str) -> LiveInfo | None:
        cid = await self.channel_id(handle)
        uploads = "UU" + cid[2:]
        pl = await self._get("playlistItems", part="contentDetails", playlistId=uploads, maxResults=5)
        ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
        if not ids:
            return None
        vids = await self._get("videos", part="snippet,liveStreamingDetails", id=",".join(ids))
        for v in vids.get("items", []):
            if v.get("snippet", {}).get("liveBroadcastContent") == "live":
                vid = v["id"]
                viewers = int(v.get("liveStreamingDetails", {}).get("concurrentViewers", 0) or 0)
                return LiveInfo("youtube", handle.lower(), v["snippet"].get("title", ""), vid,
                                f"https://www.youtube.com/watch?v={vid}", viewers)
        return None

    async def check_many(self, handles: list[str]) -> dict[str, LiveInfo | None]:
        out: dict[str, LiveInfo | None] = {}
        for h in handles:
            try:
                out[h.lower()] = await self._check_one(h)
            except Exception as e:  # unknown, leave the handle out so the watcher keeps its state
                log.warning("youtube check failed for %s: %s", h, e)
        return out
