"""Chat-only hype, end to end, on a real-time stream: audio is flat, chat explodes at stream time 28-33 s.
Checks that chat events are aligned to the right place in the recorded chunks (mtime-based wall clock +
reaction delay) and that the resulting clip is driven by the chat signal."""
import time

import httpx
import numpy as np
import pytest
import respx

from app.chat import ChatLog
from app.db import Database
from app.notifier import DiscordNotifier
from app.pipeline import Pipeline
from app.platforms.base import LiveInfo
from app.poster import PostForMe
from app.recorder import Recorder
from tests.conftest import make_synthetic_stream

pytestmark = pytest.mark.e2e


class FakePlatform:
    async def check_many(self, handles):
        return {h: LiveInfo("twitch", h, "t", "s", f"https://twitch.tv/{h}") for h in handles}


class ScriptedChat:
    """Pre-loads a ChatLog as if viewers reacted `delay` seconds after the stream moment."""

    def __init__(self, chatlog: ChatLog, delay: float, burst=(28, 33), length=45):
        self.log, self.delay, self.burst, self.length = chatlog, delay, burst, length
        self.connected = True

    def start(self):
        t0 = time.time()
        rng = np.random.default_rng(7)
        for s in range(self.length):
            n = 40 if self.burst[0] <= s < self.burst[1] else int(rng.poisson(2))
            for k in range(n):
                self.log.add(t0 + s + self.delay + k / max(n, 1) * 0.9, 1.5 if n > 20 else 1.0)

    async def stop(self):
        self.connected = False


@respx.mock
async def test_chat_burst_alone_produces_a_correctly_placed_clip(tmp_path, settings):
    respx.post("https://discord.test/h").respond(204)
    quiet = make_synthetic_stream(tmp_path / "flat.mp4", seconds=45, burst=(900, 901))
    settings.chunk_seconds = 15
    settings.warmup_s = 10
    settings.chat_delay_s = 4.0
    # -re = real-time playback, so wall-clock chat timestamps mean something
    src = ["ffmpeg", "-v", "error", "-re", "-i", str(quiet), "-c", "copy", "-f", "mpegts", "-"]
    http = httpx.AsyncClient()
    db = Database(":memory:")
    pl = Pipeline(settings, db, {"twitch": FakePlatform()}, DiscordNotifier("https://discord.test/h", http, ""),
                  PostForMe("", http), recorder_factory=lambda i, o, s: Recorder(o, src, s.chunk_seconds, s.ffmpeg_cmd),
                  chat_factory=lambda info, log: ScriptedChat(log, delay=4.0), tick=0.3)
    db.add_streamer("twitch", "chatty")
    await pl.poll_now()
    await pl.wait_idle(timeout=150)
    clips = db.list_clips()
    assert len(clips) == 1, clips
    c = clips[0]
    assert c["chat_z"] > 3 and c["audio_z"] < 2.5          # it was the chat that triggered it
    assert 26 <= c["peak_s"] <= 36                           # ...and at the right moment in the stream
    assert c["status"] == "pending"
    await http.aclose()
