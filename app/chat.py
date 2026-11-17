"""Twitch chat activity (anonymous read-only IRC, no credentials) -> per-second hype counts."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque

import numpy as np

log = logging.getLogger("clipper.chat")

HYPE_TOKENS = {
    "lul", "lulw", "kekw", "omegalul", "pog", "poggers", "pogchamp", "pogu", "monkas", "omg", "wtf",
    "lmao", "lmfao", "lol", "icant", "clip", "clipit", "w", "ratjam", "gigachad", "kappa", "ez",
    "sheesh", "cooked", "insane", "crazy", "nooo", "noooo", "wow", "huge", "dead", "bruh",
}
HYPE_EMOJI = "😂🤣🔥💀😭😱🤯👏🙌💯"
_WORD = re.compile(r"[a-z0-9]+")


def hype_weight(msg: str) -> float:
    """1 per message, +0.5 per hype token / emoji (capped), +0.5 for shouty (caps or !!!) messages."""
    low = msg.lower()
    tokens = sum(1 for w in _WORD.findall(low) if w in HYPE_TOKENS)
    tokens += sum(msg.count(ch) for ch in HYPE_EMOJI)
    w = 1.0 + 0.5 * min(tokens, 4)
    letters = [c for c in msg if c.isalpha()]
    if "!!!" in msg or (len(letters) >= 5 and sum(c.isupper() for c in letters) / len(letters) > 0.7):
        w += 0.5
    return w


def parse_privmsg(line: str) -> tuple[str, str] | None:
    """Parse ':user!user@user.tmi.twitch.tv PRIVMSG #chan :text' (optionally prefixed by @tags)."""
    if line.startswith("@"):
        _, _, line = line.partition(" ")
    if " PRIVMSG " not in line or not line.startswith(":"):
        return None
    prefix, _, rest = line[1:].partition(" PRIVMSG ")
    user = prefix.split("!", 1)[0]
    _, _, text = rest.partition(" :")
    return (user, text) if text else None


class ChatLog:
    """Time-stamped weighted message counts; query as a per-second array."""

    def __init__(self, max_age_s: float = 4 * 3600):
        self._events: deque[tuple[float, float]] = deque()
        self.max_age_s = max_age_s
        self.total = 0

    def add(self, ts: float, weight: float = 1.0) -> None:
        self._events.append((ts, weight))
        self.total += 1
        cutoff = ts - self.max_age_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def series(self, start_ts: float, n_seconds: int) -> np.ndarray:
        out = np.zeros(n_seconds, dtype=np.float64)
        for ts, w in list(self._events):
            i = int(ts - start_ts)
            if 0 <= i < n_seconds:
                out[i] += w
        return out


class TwitchChatListener:
    def __init__(self, handle: str, log_: ChatLog, host: str = "irc.chat.twitch.tv", port: int = 6697,
                 use_ssl: bool = True, clock=time.time):
        self.handle, self.chatlog, self.host, self.port, self.use_ssl = handle.lower(), log_, host, port, use_ssl
        self.clock = clock
        self._task: asyncio.Task | None = None
        self.connected = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port, ssl=self.use_ssl or None)
                nick = f"justinfan{random.randint(10000, 99999)}"
                writer.write(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{self.handle}\r\n".encode())
                await writer.drain()
                self.connected, backoff = True, 1.0
                while True:
                    raw = await reader.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("PING"):
                        writer.write(("PONG" + line[4:] + "\r\n").encode())
                        await writer.drain()
                        continue
                    msg = parse_privmsg(line)
                    if msg:
                        self.chatlog.add(self.clock(), hype_weight(msg[1]))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("chat connection to #%s lost: %s", self.handle, e)
            finally:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
