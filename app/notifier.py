"""Discord webhook notifications. Silently no-ops when no webhook is configured."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from .rating import clip_rating, rating_label

log = logging.getLogger("clipper.discord")


class DiscordNotifier:
    def __init__(self, webhook_url: str, http: httpx.AsyncClient, public_base_url: str = "",
                 max_upload_mb: float = 8.0):
        self.url, self.http = webhook_url, http
        self.public_base_url = public_base_url.rstrip("/")
        self.max_upload_bytes = int(max_upload_mb * 1024 * 1024)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def _send(self, payload: dict, file: Path | None = None) -> bool:
        if not self.enabled:
            return False
        try:
            if file is not None:
                r = await self.http.post(self.url, data={"payload_json": json.dumps(payload)},
                                         files={"file": (file.name, file.read_bytes(), "video/mp4")})
            else:
                r = await self.http.post(self.url, json=payload)
            r.raise_for_status()
            return True
        except Exception as e:
            log.warning("discord send failed: %s", e)
            return False

    async def notify_live(self, platform: str, handle: str, title: str, url: str) -> bool:
        return await self._send({"embeds": [{
            "title": f"\U0001F534 {handle} is LIVE on {platform}",
            "description": (title or "")[:200] + "\nRecording started.",
            "url": url, "color": 0xE53935}]})

    async def notify_offline(self, platform: str, handle: str, clips: int) -> bool:
        return await self._send({"content": f"⚫ **{handle}** ({platform}) went offline. {clips} clip(s) queued."})

    async def notify_clip(self, clip: dict, preview: Path | None = None) -> bool:
        review = f"{self.public_base_url}/#clip-{clip['id']}"
        length = clip["end_s"] - clip["start_s"]
        rating = clip_rating(clip.get("score"), clip.get("audio_z"), clip.get("chat_z"))
        payload = {"content": f"\U0001F3AC **New clip ready** from {clip['handle']} - review and approve:",
                   "embeds": [{"title": f"⭐ {rating:.1f}/10 ({rating_label(rating)}) · {clip['handle']} "
                                        f"clip #{clip['id']} ({length:.0f}s)", "url": review,
                               "description": f"Hype score **{clip['score']:.1f}** (audio z {clip['audio_z']:.1f}, "
                                              f"chat z {clip['chat_z']:.1f})\n[Open review page]({review})",
                               "color": 0x43A047}]}
        full = Path(clip["path"]) if clip.get("path") else None
        attach = next((p for p in (full, preview)  # full clip if it fits, otherwise the small preview
                       if p and p.exists() and p.stat().st_size <= self.max_upload_bytes), None)
        if attach and await self._send(payload, attach):
            return True
        return await self._send(payload)  # no attachment, or Discord refused it: still send the link

    async def notify_posted(self, clip: dict, ok: bool, detail: str = "") -> bool:
        icon = "✅ Posted" if ok else "❌ Post failed"
        return await self._send({"content": f"{icon}: clip #{clip['id']} ({clip['handle']}) {detail}".strip()})

    async def test(self) -> bool:
        return await self._send({"content": "✅ Clipper connected to Discord."})
