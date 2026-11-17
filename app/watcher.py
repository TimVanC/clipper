"""Polls each platform API and fires on_live / on_offline when a streamer's state changes."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .platforms.base import LiveInfo, PlatformClient

log = logging.getLogger("clipper.watcher")
Key = tuple[str, str]


class Watcher:
    def __init__(
        self,
        clients: dict[str, PlatformClient],
        get_streamers: Callable[[], list[tuple[str, str]]],
        on_live: Callable[[LiveInfo], Awaitable[None]],
        on_offline: Callable[[LiveInfo], Awaitable[None]],
        grace_polls: Callable[[], int] | int = 2,
    ):
        self.clients, self.get_streamers = clients, get_streamers
        self.on_live, self.on_offline = on_live, on_offline
        self._grace = grace_polls if callable(grace_polls) else (lambda: grace_polls)
        self.live: dict[Key, LiveInfo] = {}
        self._misses: dict[Key, int] = {}
        self.last_error: dict[str, str] = {}
        self.last_poll: float = 0.0

    async def poll_once(self) -> None:
        import time
        self.last_poll = time.time()
        wanted: dict[str, list[str]] = {}
        for plat, handle in self.get_streamers():
            wanted.setdefault(plat, []).append(handle.lower())
        wanted_keys = {(p, h) for p, hs in wanted.items() for h in hs}

        # streamers removed/disabled while live -> treat as offline
        for key in [k for k in self.live if k not in wanted_keys]:
            info = self.live.pop(key)
            self._misses.pop(key, None)
            await self._safe(self.on_offline, info)

        for plat, handles in wanted.items():
            client = self.clients.get(plat)
            if client is None:
                self.last_error[plat] = "not configured"
                continue
            try:
                res = await client.check_many(handles)
                self.last_error.pop(plat, None)
            except Exception as e:
                log.warning("%s poll failed: %s", plat, e)
                self.last_error[plat] = str(e)[:200]
                continue  # API trouble must never look like 'went offline'
            for h in handles:
                key = (plat, h)
                if h not in res:
                    continue  # unknown, keep state
                info = res[h]
                if info is not None:
                    self._misses.pop(key, None)
                    if key not in self.live:
                        self.live[key] = info
                        await self._safe(self.on_live, info)
                    else:
                        self.live[key] = info
                elif key in self.live:
                    self._misses[key] = self._misses.get(key, 0) + 1
                    if self._misses[key] >= self._grace():
                        prev = self.live.pop(key)
                        self._misses.pop(key, None)
                        await self._safe(self.on_offline, prev)

    async def _safe(self, fn, info):
        try:
            await fn(info)
        except Exception:
            log.exception("watcher callback failed for %s", info)

    async def run(self, interval: Callable[[], float]) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll loop error")
            await asyncio.sleep(interval())
