"""Orchestrator: watcher -> recorder -> detector -> clipper -> Discord -> (human approve) -> poster (app/posters)."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Callable

from .captions import build_caption
from .chat import ChatLog, TwitchChatListener
from .clipper import ChunkIndex, make_preview, make_thumb, render_clip
from .config import Settings, parse_categories, parse_start, split_list
from .facecam import choose_layout
from .db import Database
from .detector import DetectorConfig, HypeDetector, audio_db_per_second, decode_audio, SR
from .notifier import DiscordNotifier
from .platforms.base import LiveInfo, PlatformClient
from .posters import Poster
from .recorder import Recorder, streamlink_source
from .watcher import CategoryWatch, Watcher

log = logging.getLogger("clipper.pipeline")


def default_recorder_factory(info: LiveInfo, out_dir: Path, s: Settings) -> Recorder:
    return Recorder(out_dir, streamlink_source(s.streamlink_cmd, info.url), s.chunk_seconds, s.ffmpeg_cmd)


def default_chat_factory(info: LiveInfo, chatlog: ChatLog):
    return TwitchChatListener(info.handle, chatlog) if info.platform == "twitch" else None


def category_watch_from(s: Settings, now: float) -> CategoryWatch | None:
    """The category auto-watch in effect at `now`: None when it is off or its start time hasn't come yet."""
    cats = parse_categories(s.watch_categories)
    if not cats or s.category_streams <= 0:
        return None
    start = parse_start(s.category_start)
    if start is not None and now < start:
        return None
    return CategoryWatch(cats, s.category_streams, s.category_min_viewers,
                         split_list(s.category_languages), set(split_list(s.category_exclude)))


class ClipError(ValueError):
    """User-facing error for approve/reject."""


class SessionWorker:
    """Everything that happens for one live stream, from first byte to the final flush."""

    def __init__(self, pl: "Pipeline", info: LiveInfo, session_id: int, out_dir: Path):
        self.pl, self.info, self.session_id, self.dir = pl, info, session_id, out_dir
        self.stop_event = asyncio.Event()
        self.recorder: Recorder | None = None
        self.chat = None
        self.chatlog = ChatLog()
        self.detector = HypeDetector(DetectorConfig.from_settings(pl.s))
        self.index = ChunkIndex()
        self.processed = 0
        self.clips = 0
        self.task: asyncio.Task | None = None

    def request_stop(self) -> None:
        self.stop_event.set()

    async def run(self) -> None:
        pl = self.pl
        try:
            self.recorder = pl.recorder_factory(self.info, self.dir / "raw", pl.s)
            self.chat = pl.chat_factory(self.info, self.chatlog)
            if self.chat:
                self.chat.start()
            await self.recorder.start()
            while True:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), pl.tick)
                except asyncio.TimeoutError:
                    pass
                await self._drain(final=False)
                if self.stop_event.is_set() or self.recorder.finished:
                    break
            await self.recorder.stop()
            await self._drain(final=True)
            await self._render(self.detector.pop_ready(final=True))
        except Exception:
            log.exception("session %s crashed", self.session_id)
        finally:
            if self.chat:
                await self.chat.stop()
            if not self.stop_event.is_set():  # recorder ended on its own: let the watcher re-detect
                pl.watcher.live.pop((self.info.platform, self.info.handle.lower()), None)
            pl.db.end_session(self.session_id)
            if pl.s.delete_raw:
                shutil.rmtree(self.dir / "raw", ignore_errors=True)
            pl.workers.pop((self.info.platform, self.info.handle.lower()), None)
            await pl.notifier.notify_offline(self.info.platform, self.info.handle, self.clips)

    async def _drain(self, final: bool) -> None:
        chunks = self.recorder.completed_chunks() if not final else sorted((self.dir / "raw").glob("chunk_*.ts"))
        chunks = [c for c in chunks if c.stat().st_size > 0]
        for path in chunks[self.processed:]:
            self.processed += 1
            try:
                await self._process_chunk(path)
            except Exception:
                log.exception("chunk %s failed", path)

    async def _process_chunk(self, path: Path) -> None:
        pl = self.pl
        self.detector.cfg = DetectorConfig.from_settings(pl.s)
        samples = await asyncio.to_thread(decode_audio, str(path), pl.s.ffmpeg_cmd)
        db = audio_db_per_second(samples)
        if len(db) == 0:
            log.warning("no audio in %s, skipping", path.name)
            return
        wall_start = path.stat().st_mtime - len(samples) / SR
        chat = None
        if self.chatlog.total > 0:  # chat reacts a few seconds after the moment that caused it
            chat = self.chatlog.series(wall_start + pl.s.chat_delay_s, len(db))
        n = self.detector.ingest(db, chat)
        self.index.add(str(path), n)
        await self._render(self.detector.pop_ready(final=False))
        self._cleanup_old_chunks()

    def _cleanup_old_chunks(self) -> None:
        if not self.pl.s.delete_raw:
            return
        keep_from = self.detector._scanned - (self.pl.s.max_clip_s + self.pl.s.pre_roll_s + 120)
        for c in self.index.chunks:
            if c.start + c.length <= keep_from:
                Path(c.path).unlink(missing_ok=True)

    async def _render(self, candidates) -> None:
        pl = self.pl
        for cand in candidates:
            clip_id = pl.db.add_clip(self.session_id, self.info.platform, self.info.handle, cand.start_s,
                                     cand.end_s, cand.peak_s, cand.score, cand.audio_z, cand.chat_z)
            out = pl.clips_dir / f"{clip_id}.mp4"
            thumb = pl.clips_dir / f"{clip_id}.jpg"
            # the watcher holds the newest title/category: the stream may have switched games since it started
            now = pl.watcher.live.get((self.info.platform, self.info.handle.lower()), self.info)
            try:
                pieces = self.index.pieces(cand.start_s, cand.end_s)
                layout, split = await asyncio.to_thread(choose_layout, pieces, pl.s.layout, now.game,
                                                        self.info.handle, pl.s.facecam_boxes, pl.s.ffmpeg_cmd)
                await asyncio.to_thread(render_clip, pieces, out, layout, pl.s.ffmpeg_cmd, split)
                try:
                    await asyncio.to_thread(make_thumb, out, thumb, pl.s.ffmpeg_cmd, min(3.0, (cand.end_s - cand.start_s) / 2))
                except Exception:
                    thumb = Path("")
                caption = build_caption(pl.s.caption_template, handle=self.info.handle, platform=self.info.platform,
                                        name=now.name or self.info.name, title=now.title, game=now.game,
                                        seed=clip_id, hooks=pl.s.caption_hooks, extra_tags=pl.s.hashtags,
                                        max_tags=pl.s.max_hashtags)
                pl.db.update_clip(clip_id, path=str(out), thumb=str(thumb) if thumb else "", status="pending",
                                  caption=caption)
                self.clips += 1
            except Exception as e:
                log.exception("render failed for clip %s", clip_id)
                pl.db.update_clip(clip_id, status="failed", post_error=f"render failed: {e}"[:300])
                continue
            clip = pl.db.get_clip(clip_id)
            preview: Path | None = pl.clips_dir / f"{clip_id}_preview.mp4"
            try:
                await asyncio.to_thread(make_preview, out, preview, pl.s.ffmpeg_cmd, pl.notifier.max_upload_bytes)
            except Exception:
                log.warning("preview failed for clip %s", clip_id)
                preview = None
            await pl.notifier.notify_clip(clip, preview)
            if preview:
                preview.unlink(missing_ok=True)
            if pl.s.auto_approve and (pl.poster.enabled or pl.poster.manual):
                try:
                    await pl.approve(clip_id)
                except ClipError as e:
                    log.warning("auto-approve skipped: %s", e)


class Pipeline:
    def __init__(self, settings: Settings, db: Database, clients: dict[str, PlatformClient],
                 notifier: DiscordNotifier, poster: Poster, *,
                 recorder_factory: Callable = default_recorder_factory,
                 chat_factory: Callable = default_chat_factory, tick: float = 3.0):
        self.s, self.db, self.notifier, self.poster = settings, db, notifier, poster
        self.recorder_factory, self.chat_factory, self.tick = recorder_factory, chat_factory, tick
        self.clips_dir = settings.data_dir / "clips"
        self.sessions_dir = settings.data_dir / "sessions"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.workers: dict[tuple[str, str], SessionWorker] = {}
        self._bg: set[asyncio.Task] = set()
        self._last_live_notice: dict[tuple[str, str], float] = {}
        self._watch_task: asyncio.Task | None = None
        self.watcher = Watcher(
            clients,
            lambda: [(r["platform"], r["handle"]) for r in db.list_streamers(enabled_only=True)],
            self.on_live, self.on_offline, grace_polls=lambda: self.s.offline_grace_polls,
            category_watch=self.category_watch)

    def category_watch(self) -> CategoryWatch | None:
        return category_watch_from(self.s, time.time())

    # ---- lifecycle -------------------------------------------------
    def load_overrides(self) -> None:
        for k, v in self.db.get_settings().items():
            try:
                self.s.update({k: v})
            except ValueError:
                log.warning("ignoring bad saved setting %s=%r", k, v)

    async def start(self) -> None:
        self.load_overrides()
        stuck = self.db.fail_interrupted_posts()
        if stuck:
            log.warning("clips %s were posting when the app stopped; marked failed for review", stuck)
        self._watch_task = asyncio.create_task(self.watcher.run(lambda: self.s.poll_seconds))

    async def stop(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        for w in list(self.workers.values()):
            w.request_stop()
        await self.wait_idle()

    async def wait_idle(self, timeout: float = 120.0) -> None:
        deadline = time.time() + timeout
        while (self.workers or self._bg) and time.time() < deadline:
            tasks = [w.task for w in self.workers.values() if w.task] + list(self._bg)
            if tasks:
                await asyncio.wait(tasks, timeout=0.5)
            else:
                await asyncio.sleep(0.05)

    # ---- watcher callbacks -------------------------------------------------
    async def on_live(self, info: LiveInfo) -> None:
        key = (info.platform, info.handle.lower())
        if key in self.workers:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", info.handle)
        out_dir = self.sessions_dir / f"{info.platform}_{safe}_{stamp}"
        out_dir.mkdir(parents=True, exist_ok=True)
        sid = self.db.start_session(info.platform, info.handle, info.title, info.stream_id, str(out_dir))
        worker = SessionWorker(self, info, sid, out_dir)
        self.workers[key] = worker
        worker.task = asyncio.create_task(worker.run())
        if time.time() - self._last_live_notice.get(key, 0) > 600:
            self._last_live_notice[key] = time.time()
            await self.notifier.notify_live(info.platform, info.handle, info.title, info.url)

    async def on_offline(self, info: LiveInfo) -> None:
        w = self.workers.get((info.platform, info.handle.lower()))
        if w:
            w.request_stop()

    async def poll_now(self) -> None:
        await self.watcher.poll_once()

    # ---- review actions -------------------------------------------------
    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def approve(self, clip_id: int, caption: str | None = None, accounts: list[str] | None = None) -> dict:
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ClipError("clip not found")
        manual = self.poster.manual
        if not manual and not self.poster.enabled:
            raise ClipError(f"{self.poster.label} is not configured (set {self.poster.env_key})")
        if not clip.get("path") or not Path(clip["path"]).exists():
            raise ClipError("clip has no video file (render failed or it was rejected)")
        if manual:  # POSTER_PROVIDER=none: nothing is posted, the GUI offers the file as a download
            if not self.db.claim_clip(clip_id, ("pending", "failed"), "approved"):
                raise ClipError(f"clip is {clip['status']}, cannot approve")
            cap = caption if caption is not None and caption.strip() else (clip["caption"] or "")
            self.db.update_clip(clip_id, caption=cap, post_error="")
            return self.db.get_clip(clip_id)  # type: ignore[return-value]
        accts = accounts if accounts is not None else self.s.default_account_ids()
        if not accts:
            raise ClipError("select at least one social account (or set default accounts in Settings)")
        if self.db.count_posted_since(time.time() - 86400) >= self.s.max_posts_per_day:
            raise ClipError(f"daily post cap reached ({self.s.max_posts_per_day})")
        if not self.db.claim_clip(clip_id, ("pending", "failed"), "posting"):
            raise ClipError(f"clip is {clip['status']}, cannot approve")
        cap = caption if caption is not None and caption.strip() else (clip["caption"] or "")
        self.db.update_clip(clip_id, caption=cap, accounts=",".join(accts), post_error="")
        self._spawn(self._do_post(clip_id))
        return self.db.get_clip(clip_id)  # type: ignore[return-value]

    async def _do_post(self, clip_id: int) -> None:
        clip = self.db.get_clip(clip_id)
        try:
            res = await self.poster.publish(Path(clip["path"]), clip["caption"], clip["accounts"].split(","),
                                            external_id=f"clip-{clip_id}")
            partial = res.get("partial_error", "")  # combined providers: some posted, some failed
            self.db.update_clip(clip_id, status="posted", post_id=res["post_id"], posted_at=time.time(),
                                post_error=partial[:300])
            if partial:
                await self.notifier.notify_posted(clip, False, f"partly posted: {partial}"[:300])
            else:
                await self.notifier.notify_posted(clip, True, f"-> {len(clip['accounts'].split(','))} account(s)")
        except Exception as e:
            log.exception("posting clip %s failed", clip_id)
            self.db.update_clip(clip_id, status="failed", post_error=str(e)[:300])
            await self.notifier.notify_posted(clip, False, str(e)[:150])

    def reject(self, clip_id: int) -> dict:
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ClipError("clip not found")
        if not self.db.claim_clip(clip_id, ("pending", "failed"), "rejected"):
            raise ClipError(f"clip is {clip['status']}, cannot reject")
        for key in ("path", "thumb"):
            if clip.get(key):
                Path(clip[key]).unlink(missing_ok=True)
        return self.db.get_clip(clip_id)  # type: ignore[return-value]

    # ---- state for GUI -------------------------------------------------
    def state(self) -> dict:
        live = []
        for (plat, handle), info in self.watcher.live.items():
            w = self.workers.get((plat, handle))
            live.append({"platform": plat, "handle": handle, "title": info.title, "viewers": info.viewers,
                         "url": info.url, "recording": bool(w and w.recorder and not w.recorder.finished),
                         "chunks": w.processed if w else 0, "clips": w.clips if w else 0,
                         "chat": bool(w and w.chat and getattr(w.chat, "connected", False)),
                         "game": info.game, "auto": self.watcher.auto.get((plat, handle), "")})
        return {"live": live, "streamers": self.db.list_streamers(), "counts": self.db.counts(),
                "configured": self.s.configured(), "poster": self.poster.info(),
                "category_start_ts": parse_start(self.s.category_start), "watcher": {"last_poll": self.watcher.last_poll,
                                                               "errors": self.watcher.last_error}}
