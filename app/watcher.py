"""Polls each platform API and fires on_live / on_offline when a streamer's state changes."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .captions import same_category
from .platforms.base import LiveInfo, PlatformClient

log = logging.getLogger("clipper.watcher")
Key = tuple[str, str]


@dataclass
class CategoryWatch:
    """Record the most-watched streams in a category, whoever is streaming it.

    Up to `streams` are recorded at once, across all platforms. A stream keeps its slot until it ends or moves
    to another category; falling a few places in the ranking does not stop it."""
    categories: list[tuple[str | None, str]] = field(default_factory=list)  # (platform or None = all, name)
    streams: int = 3
    min_viewers: int = 1000
    languages: list[str] = field(default_factory=list)
    exclude: set[str] = field(default_factory=set)

    def matches(self, platform: str, game: str) -> str | None:
        """The watched category this game belongs to, or None."""
        for plat, name in self.categories:
            if (plat is None or plat == platform) and same_category(game, name):
                return name
        return None


class Watcher:
    def __init__(
        self,
        clients: dict[str, PlatformClient],
        get_streamers: Callable[[], list[tuple[str, str]]],
        on_live: Callable[[LiveInfo], Awaitable[None]],
        on_offline: Callable[[LiveInfo], Awaitable[None]],
        grace_polls: Callable[[], int] | int = 2,
        category_watch: Callable[[], CategoryWatch | None] | None = None,
    ):
        self.clients, self.get_streamers = clients, get_streamers
        self.on_live, self.on_offline = on_live, on_offline
        self._grace = grace_polls if callable(grace_polls) else (lambda: grace_polls)
        self.category_watch = category_watch or (lambda: None)
        self.live: dict[Key, LiveInfo] = {}
        self.auto: dict[Key, str] = {}  # streams picked from a watched category -> that category
        self._misses: dict[Key, int] = {}
        self.last_error: dict[str, str] = {}
        self.last_poll: float = 0.0

    async def _pick_from_categories(self, cw: CategoryWatch | None, explicit: set[Key]) -> None:
        """Drop picks the settings no longer allow, then fill free slots with the biggest streams."""
        for key, cat in list(self.auto.items()):
            if cw is None or key[1] in cw.exclude or key in explicit or cw.matches(key[0], cat) is None:
                self.auto.pop(key)
        if cw is None or not cw.categories:
            return
        found: list[tuple[LiveInfo, str]] = []
        for plat_filter, name in cw.categories:
            for plat, client in self.clients.items():
                if (plat_filter and plat != plat_filter) or not hasattr(client, "top_streams"):
                    continue
                err_key = f"{plat} category"
                try:
                    found += [(i, name) for i in await client.top_streams(name, cw.languages)]
                    self.last_error.pop(err_key, None)
                except Exception as e:
                    log.warning("%s category %r failed: %s", plat, name, e)
                    self.last_error[err_key] = str(e)[:200]
        for info, name in sorted(found, key=lambda f: -f[0].viewers):
            if len(self.auto) >= cw.streams or info.viewers < cw.min_viewers:
                break
            key = (info.platform, info.handle.lower())
            if key not in explicit and key not in self.auto and key[1] not in cw.exclude:
                log.info("auto-watching %s/%s (%d viewers in %s)", *key, info.viewers, name)
                self.auto[key] = name

    async def poll_once(self) -> None:
        import time
        self.last_poll = time.time()
        wanted: dict[str, list[str]] = {}
        for plat, handle in self.get_streamers():
            wanted.setdefault(plat, []).append(handle.lower())
        explicit = {(p, h) for p, hs in wanted.items() for h in hs}
        try:
            cw = self.category_watch()
        except Exception:
            log.exception("bad category watch settings")
            cw = None
        await self._pick_from_categories(cw, explicit)
        for plat, h in self.auto:
            wanted.setdefault(plat, []).append(h)
        wanted_keys = explicit | set(self.auto)

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
                if key in self.auto and info is not None and info.game \
                        and (cw is None or cw.matches(plat, info.game) is None):
                    log.info("%s/%s moved to %r, no longer auto-watched", plat, h, info.game)
                    info = None  # switched games: stop now, no grace period
                    self.auto.pop(key, None)
                    if key in self.live:
                        self._misses.pop(key, None)
                        await self._safe(self.on_offline, self.live.pop(key))
                    continue
                if key in self.auto and info is None and key not in self.live:
                    self.auto.pop(key, None)  # gone before it was ever picked up
                    continue
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
                        self.auto.pop(key, None)  # frees the slot for the next biggest stream
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
