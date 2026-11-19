"""Orchestrator: watcher -> recorder -> detector -> clipper -> Discord -> (human approve) -> poster (app/posters)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable

from .captions import build_caption
from .chat import ChatLog, TwitchChatListener
from .clipper import ChunkIndex, make_preview, make_thumb, render_clip
from .config import Settings, parse_categories, parse_start, split_list
from . import headline
from .facecam import choose_layout
from .db import COUNTED_STATES, Database
from .detector import DetectorConfig, HypeDetector, audio_db_per_second, decode_audio, SR
from .notifier import DiscordNotifier
from .platforms.base import LiveInfo, PlatformClient
from .posters import Poster
from .posters.base import is_limit
from .rating import clip_rating
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


VERSIONS = ("headline", "plain", "full")

# posts per connected account per rolling 24h that Upload-Post lets through (docs.upload-post.com/guides/limit-of-uploads);
# the networks themselves allow less (see tiktok_per_day / youtube_per_day)
NETWORK_CAPS = {"tiktok": 15, "youtube": 30, "instagram": 50, "facebook": 25, "threads": 50, "linkedin": 150,
                "pinterest": 20, "x": 10, "bluesky": 25}


LIMIT_BACKOFF_S = 3 * 3600   # an account a network turned down for a daily limit rests this long
CHECK_GIVE_UP_S = 6 * 3600   # an upload the network still hasn't answered for after this is failed
QUEUE_TICK_S = 60.0          # how often the queue posts and checks uploads


def format_plan(jobs: list[tuple[str, str]]) -> str:
    return ",".join(f"{v}>{a}" for v, a in jobs)


def parse_plan(text: str) -> list[tuple[str, str]]:
    return [tuple(x.split(">", 1)) for x in (text or "").split(",") if ">" in x]  # type: ignore[misc]


def network_of(account: str) -> str:
    """'default:tiktok' / 'uploadpost/default:youtube' -> 'tiktok' / 'youtube' (the id itself if no network)."""
    return account.rsplit(":", 1)[-1].rsplit("/", 1)[-1].lower()


def _as_hook(text: str) -> str:
    """The headline as the caption's first line (braces escaped for the caption template)."""
    return text.replace("{", "{{").replace("}", "}}") if text else ""


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
        self.walls: dict[int, float] = {}  # chunk start on the session timeline -> wall-clock time
        self.processed = 0
        self.seen: set[str] = set()  # chunk names already analysed (older ones get deleted, so not by position)
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
                try:
                    self.dir.rmdir()  # nothing else is kept per session
                except OSError:
                    pass
            pl.workers.pop((self.info.platform, self.info.handle.lower()), None)
            await pl.notifier.notify_offline(self.info.platform, self.info.handle, self.clips)

    async def _drain(self, final: bool) -> None:
        chunks = self.recorder.completed_chunks() if not final else sorted((self.dir / "raw").glob("chunk_*.ts"))
        for path in chunks:
            if path.name in self.seen:
                continue
            try:
                if path.stat().st_size == 0:
                    continue
            except FileNotFoundError:  # dropped by the recordings size cap before it was analysed
                continue
            self.seen.add(path.name)
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
        ref = self.index.add(str(path), n)
        self.walls[ref.start] = wall_start
        await self._render(self.detector.pop_ready(final=False))
        self._cleanup_old_chunks()

    def _wall(self, t: float) -> float | None:
        """Wall-clock time of a point on the session timeline (for matching chat messages)."""
        for c in self.index.chunks:
            if c.start <= t < c.start + c.length and c.start in self.walls:
                return self.walls[c.start] + (t - c.start)
        return None

    def _chat_near(self, peak_s: float) -> list[str]:
        w = self._wall(peak_s)
        if w is None or self.chatlog.total == 0:
            return []
        return self.chatlog.messages(w - 10, w + self.pl.s.chat_delay_s + 15)

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
            base = pl.base_path(clip_id)
            thumb = pl.clips_dir / f"{clip_id}.jpg"
            # the watcher holds the newest title/category: the stream may have switched games since it started
            now = pl.watcher.live.get((self.info.platform, self.info.handle.lower()), self.info)
            name = now.name or self.info.name
            try:
                pieces = self.index.pieces(cand.start_s, cand.end_s)
                layout, split = await asyncio.to_thread(choose_layout, pieces, pl.s.layout, now.game,
                                                        self.info.handle, pl.s.facecam_boxes, pl.s.ffmpeg_cmd)
                await asyncio.to_thread(render_clip, pieces, base, layout, pl.s.ffmpeg_cmd, split)
                text, transcript = await pl.make_headline(base, cand.peak_s - cand.start_s, name or self.info.handle,
                                                          now.game, now.title, self._chat_near(cand.peak_s))
                seam = headline.seam_y(layout)
                try:
                    await asyncio.to_thread(headline.apply, base, out, text, seam, pl.s.ffmpeg_cmd)
                except Exception:
                    log.exception("headline overlay failed for clip %s; posting it without", clip_id)
                    text = ""
                    await asyncio.to_thread(headline.apply, base, out, "", seam, pl.s.ffmpeg_cmd)
                await pl.make_full_version(clip_id, pieces)
                try:
                    await asyncio.to_thread(make_thumb, out, thumb, pl.s.ffmpeg_cmd, min(3.0, (cand.end_s - cand.start_s) / 2))
                except Exception:
                    thumb = Path("")
                caption = build_caption(pl.s.caption_template, handle=self.info.handle, platform=self.info.platform,
                                        name=name, title=now.title, game=now.game, seed=clip_id,
                                        hooks=_as_hook(text) or pl.s.caption_hooks, extra_tags=pl.s.hashtags,
                                        max_tags=pl.s.max_hashtags)
                pl.db.update_clip(clip_id, path=str(out), thumb=str(thumb) if thumb else "", status="pending",
                                  caption=caption, headline=text, seam=seam, transcript=transcript[:4000])
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
            rating = clip_rating(clip["score"], clip["audio_z"], clip["chat_z"])
            if pl.should_auto_post(rating):
                try:
                    log.info("auto-posting clip %s (rating %.1f)", clip_id, rating)
                    await pl.approve(clip_id, version=pl.s.auto_post_version)
                except ClipError as e:
                    log.warning("auto-post skipped for clip %s: %s", clip_id, e)


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
        self._queue_task: asyncio.Task | None = None
        self._queue_lock = asyncio.Lock()
        self.disk: dict[str, float] = {}
        self._claude = None  # anthropic.AsyncAnthropic, made on first use
        self.watcher = Watcher(
            clients,
            lambda: [(r["platform"], r["handle"]) for r in db.list_streamers(enabled_only=True)],
            self.on_live, self.on_offline, grace_polls=lambda: self.s.offline_grace_polls,
            category_watch=self.category_watch)

    # ---- on-screen headline -------------------------------------------------
    def base_path(self, clip_id: int) -> Path:
        """The clip without its headline, kept so the headline can be changed later."""
        return self.clips_dir / f"{clip_id}_base.mp4"

    def full_path(self, clip_id: int, base: bool = False) -> Path:
        """The full-frame version: the whole stream frame on black, no text. (base=True: the text-free
        render older clips kept next to a titled <id>_full.mp4.)"""
        return self.clips_dir / f"{clip_id}_full{'_base' if base else ''}.mp4"

    def full_file(self, clip_id: int) -> Path | None:
        """The full-frame file to show and post: always the text-free one."""
        for p in (self.full_path(clip_id, base=True), self.full_path(clip_id)):
            if p.exists():
                return p
        return None

    def version_file(self, clip: dict, version: str) -> Path:
        """headline: the main clip | plain: same layout, no text | full: the full frame, no text."""
        cid = clip["id"]
        if version == "plain" and self.base_path(cid).exists():
            return self.base_path(cid)
        if version == "full" and self.full_file(cid):
            return self.full_file(cid)  # type: ignore[return-value]
        if version not in VERSIONS:
            raise ClipError(f"version must be one of {', '.join(VERSIONS)}")
        return Path(clip["path"])

    async def make_full_version(self, clip_id: int, pieces) -> None:
        """Render the full-frame version (no text) from the source pieces, while they still exist."""
        if not self.wants_full():
            return
        full = self.full_path(clip_id)
        try:
            await asyncio.to_thread(render_clip, pieces, full, "classic", self.s.ffmpeg_cmd)
        except Exception:
            log.exception("full-frame version failed for clip %s", clip_id)
            full.unlink(missing_ok=True)

    def wants_full(self) -> bool:
        """Render the full-frame version only when a network would post it (saves a render and disk)."""
        if not self.s.full_frame_version:
            return False
        auto = self.s.auto_post_version
        return auto == "full" or (auto == "all" and bool({self.s.tiktok_version, self.s.youtube_version} & {"full", "all"}))

    # ---- disk -------------------------------------------------
    def clip_files(self, clip: dict) -> list[Path]:
        cid = clip["id"]
        files = [self.base_path(cid), self.full_path(cid), self.full_path(cid, base=True),
                 self.clips_dir / f"{cid}_preview.mp4"]
        return ([Path(clip["path"])] if clip.get("path") else []) + files

    def drop_clip_files(self, clip: dict) -> int:
        """Delete a clip's videos (the small thumbnail stays for History). Returns the bytes freed."""
        freed = 0
        for f in self.clip_files(clip):
            try:
                size = f.stat().st_size
                f.unlink()
                freed += size
            except FileNotFoundError:
                pass
            except OSError as e:  # e.g. being served to the browser right now: next pass
                log.warning("couldn't delete %s: %s", f, e)
        self.db.update_clip(clip["id"], path="")
        return freed

    def _expire(self, clip: dict, why: str) -> int:
        if not self.db.claim_clip(clip["id"], ("pending", "failed"), "expired"):
            return 0  # approved (or rejected) meanwhile
        self.db.update_clip(clip["id"], post_error=why)
        return self.drop_clip_files(clip)

    @staticmethod
    def _dir_bytes(paths) -> int:
        total = 0
        for f in paths:
            try:
                total += f.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def sweep_recordings(self) -> int:
        """Startup: nothing is recording yet, so every recorded chunk on disk is a leftover. Returns bytes freed."""
        freed = 0
        for d in list(self.sessions_dir.iterdir()):
            if not d.is_dir():
                continue
            raw = d / "raw"
            if raw.is_dir():
                freed += self._dir_bytes(raw.rglob("*"))
                shutil.rmtree(raw, ignore_errors=True)
            try:
                d.rmdir()
            except OSError:
                pass
        return freed

    def enforce_buffer_cap(self) -> int:
        """Recordings over max_buffer_gb: delete the oldest finished chunks (never one being written)."""
        cap = self.s.max_buffer_gb * 1024 ** 3
        old, total = [], 0
        for raw in self.sessions_dir.glob("*/raw"):
            chunks = sorted(raw.glob("chunk_*.ts"))
            for i, f in enumerate(chunks):
                try:
                    st = f.stat()
                except FileNotFoundError:
                    continue
                total += st.st_size
                if i < len(chunks) - 1:
                    old.append((st.st_mtime, st.st_size, f))
        freed = 0
        for _, size, f in sorted(old):
            if total <= cap:
                break
            f.unlink(missing_ok=True)
            total -= size
            freed += size
        if freed:
            log.warning("recordings over %.1f GB: deleted %.1f GB of the oldest chunks", self.s.max_buffer_gb,
                        freed / 1024 ** 3)
        self.disk["recordings_gb"] = round(total / 1024 ** 3, 2)
        return freed

    def clean_clips(self) -> int:
        """Posted clips' files go; clips not posted or approved within clip_keep_hours expire; past
        max_clips_gb the oldest unposted clips expire first. Queued clips keep their files. Returns bytes freed."""
        freed = sum(self.drop_clip_files(c) for c in self.db.clips_with_files(("posted",)))
        now, keep = time.time(), self.s.clip_keep_hours * 3600
        for c in self.db.clips_with_files(("pending", "failed")):  # oldest first
            if not Path(c["path"]).exists():  # deleted outside the app: nothing left to review or post
                self._expire(c, "video file is missing (deleted outside the app)")
            elif now - max(c["created_at"] or 0, c["decided_at"] or 0) > keep:
                freed += self._expire(c, f"not posted within {self.s.clip_keep_hours:g}h: video deleted to save space")
        total = self._dir_bytes(Path(e.path) for e in os.scandir(self.clips_dir) if e.is_file())
        for c in self.db.clips_with_files(("pending", "failed")):
            if total <= self.s.max_clips_gb * 1024 ** 3:
                break
            got = self._expire(c, f"video deleted to keep clips under {self.s.max_clips_gb:g} GB")
            total -= got
            freed += got
        self.disk["clips_gb"] = round(total / 1024 ** 3, 2)
        return freed

    def tidy_disk(self) -> int:
        return self.enforce_buffer_cap() + self.clean_clips()

    def headline_writer(self) -> str:
        mode = self.s.headline_mode
        if mode == "auto":  # the free pick can't tell the streamer from game dialogue: off unless asked for
            return "claude" if self.s.anthropic_api_key else "off"
        return mode

    async def make_headline(self, video: Path, peak_s: float, streamer: str, game: str = "", title: str = "",
                            chat: list[str] | None = None) -> tuple[str, str]:
        """(headline, transcript) for a rendered clip. Never raises: a failure means no headline."""
        writer = self.headline_writer()
        if writer == "off":
            return "", ""
        try:
            segments = await asyncio.to_thread(headline.transcribe, video, self.s.whisper_model)
        except Exception:
            log.exception("transcription failed for %s", video)
            return "", ""
        transcript = headline.transcript_text(segments)
        if writer == "claude" and self.s.anthropic_api_key:
            try:
                if self._claude is None:
                    import anthropic
                    self._claude = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key)
                return await headline.claude_headline(self._claude, segments, peak_s, streamer, game, title,
                                                      chat), transcript
            except Exception:
                log.exception("Claude headline failed; using the free pick")
        return headline.local_headline(segments, peak_s), transcript

    async def set_headline(self, clip_id: int, text: str) -> dict:
        """Re-burn a (changed) headline into a clip from its text-free render."""
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ClipError("clip not found")
        text = headline.clean(text)[:120]
        base = self.base_path(clip_id)
        if not base.exists():
            raise ClipError("this clip was made before headlines existed, so its text can't be changed")
        await asyncio.to_thread(headline.apply, base, clip["path"], text, clip.get("seam") or 768, self.s.ffmpeg_cmd)
        if clip.get("thumb"):
            try:
                await asyncio.to_thread(make_thumb, clip["path"], clip["thumb"], self.s.ffmpeg_cmd, 3.0)
            except Exception:
                pass
        self.db.update_clip(clip_id, headline=text)
        return self.db.get_clip(clip_id)  # type: ignore[return-value]

    def cap_for(self, account: str) -> int:
        """Uploads `account` may get per rolling 24h."""
        net = network_of(account)
        own = {"tiktok": self.s.tiktok_per_day, "youtube": self.s.youtube_per_day}.get(net)
        if own is not None:
            return min(own, NETWORK_CAPS[net])
        return min(NETWORK_CAPS.get(net, self.s.max_posts_per_day), self.s.max_posts_per_day)

    def version_for(self, network: str) -> str:
        """Posting all versions: the one this network gets, or 'all'."""
        return {"tiktok": self.s.tiktok_version, "youtube": self.s.youtube_version}.get(network, "all")

    def jobs_for(self, clip: dict, version: str, accounts: list[str]) -> list[tuple[str, str]]:
        """[(version, account)]: one upload each. Posting 'all', each network gets its own pick."""
        names = [n for n, _ in self.posting_files(clip, version)]
        jobs = []
        for a in accounts:
            pick = self.version_for(network_of(a)) if version == "all" else "all"
            wanted = names if pick == "all" else [pick if pick in names else names[0]]
            jobs += [(n, a) for n in wanted]
        return jobs

    def slot_at(self, account: str, now: float | None = None) -> float:
        """When `account` may get its next upload (<= now: right away): its daily allowance, the gap between
        uploads, and a rest after a network turned an upload down for a daily limit."""
        now = time.time() if now is None else now
        rows = self.db.account_uploads_since(account, now - 86400)
        counted = [u["sent_at"] for u in rows if u["state"] in COUNTED_STATES]
        at, cap = 0.0, self.cap_for(account)
        if len(counted) >= cap:  # full: free again when enough of the last 24h's uploads age out
            at = counted[len(counted) - cap] + 86400
        if counted:
            at = max(at, counted[-1] + self.s.post_gap_min * 60)
        limited = [u["sent_at"] for u in rows if u["state"] == "limited"]
        if limited:
            at = max(at, limited[-1] + LIMIT_BACKOFF_S)
        return at

    def posting_slots(self) -> list[dict]:
        """Per default account: uploads in the last 24h, the allowance, and when the next one can go out."""
        now = time.time()
        out = []
        for a in self.s.default_account_ids():
            used = sum(u["state"] in COUNTED_STATES for u in self.db.account_uploads_since(a, now - 86400))
            out.append({"account": a, "network": network_of(a), "used": used, "cap": self.cap_for(a),
                        "next": max(self.slot_at(a, now), now)})
        return out

    @staticmethod
    def rating(clip: dict) -> float:
        return clip_rating(clip["score"], clip["audio_z"], clip["chat_z"])

    @staticmethod
    def remaining(clip: dict, uploads: list[dict]) -> list[tuple[str, str]]:
        """Planned uploads not made yet (one turned down for a daily limit is still to do)."""
        done = {(u["version"], u["account"]) for u in uploads if u["state"] != "limited"}
        return [j for j in parse_plan(clip["plan"]) if j not in done]

    def should_auto_post(self, rating: float) -> bool:
        """auto_approve posts every clip; auto_post_min_rating > 0 posts the ones rated at least that."""
        if not (self.poster.enabled or self.poster.manual):
            return False
        floor = self.s.auto_post_min_rating
        return self.s.auto_approve or (floor > 0 and rating >= floor)

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
        cut = self.db.fail_interrupted_renders()
        if cut:
            log.warning("clips %s were rendering when the app stopped; marked failed", cut)
        self.db.end_open_sessions()
        freed = await asyncio.to_thread(self.sweep_recordings)
        if freed:
            log.warning("deleted %.1f GB of recordings left over from earlier runs", freed / 1024 ** 3)
        cut_off = self.db.fail_interrupted_uploads()
        if cut_off:
            log.warning("clips %s were uploading when the app stopped; those uploads are marked failed", cut_off)
        self._watch_task = asyncio.create_task(self.watcher.run(lambda: self.s.poll_seconds))
        self._queue_task = asyncio.create_task(self._housekeeping_loop())

    async def stop(self) -> None:
        for task in (self._watch_task, self._queue_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for w in list(self.workers.values()):
            w.request_stop()
        await self.wait_idle(timeout=900)  # the last clips (headline + three versions) take a few minutes each

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

    async def approve(self, clip_id: int, caption: str | None = None, accounts: list[str] | None = None,
                      on_screen: str | None = None, plain: bool = False, version: str = "headline") -> dict:
        """version: which file to post - headline (main), plain (no text) or full (full frame); plain=True is
        the same as version='plain'."""
        version = "plain" if plain else version
        if version not in VERSIONS + ("all",):
            raise ClipError(f"version must be one of {', '.join(VERSIONS)} or all")
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ClipError("clip not found")
        if clip["status"] not in ("pending", "failed"):  # before the file check: a posted clip has no file left
            raise ClipError(f"clip is {clip['status']}, cannot approve")
        if (on_screen is not None and version != "plain" and clip["status"] in ("pending", "failed")
                and headline.clean(on_screen) != (clip.get("headline") or "")
                and self.base_path(clip_id).exists()):
            clip = await self.set_headline(clip_id, on_screen)  # edited in Review: burn the new text in first
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
        jobs = self.jobs_for(clip, version, accts)
        if not self.db.claim_clip(clip_id, ("pending", "failed"), "queued"):
            raise ClipError(f"clip is {clip['status']}, cannot approve")
        cap = caption if caption is not None and caption.strip() else (clip["caption"] or "")
        self.db.clear_failed_uploads(clip_id)
        self.db.update_clip(clip_id, caption=cap, accounts=",".join(accts), post_error="", plan=format_plan(jobs))
        self._spawn(self.run_queue())  # whatever has a free slot goes out now, the rest waits its turn
        return self.db.get_clip(clip_id)  # type: ignore[return-value]

    def posting_files(self, clip: dict, version: str) -> list[tuple[str, Path]]:
        """[(version, file)] to post. 'all': every distinct version this clip has (with headline, without,
        full frame); a clip without a headline has no separate 'without' version."""
        if version != "all":
            return [(version, self.version_file(clip, version))]
        out = [("headline", Path(clip["path"]))]
        if clip.get("headline") and self.base_path(clip["id"]).exists():
            out.append(("plain", self.base_path(clip["id"])))
        if self.full_file(clip["id"]):
            out.append(("full", self.full_file(clip["id"])))
        return out

    # ---- posting queue -------------------------------------------------
    async def _housekeeping_loop(self) -> None:
        """Every minute: post what has a free slot, check uploads, keep the disk within its limits."""
        while True:
            try:
                await self.check_uploads()
            except Exception:
                log.exception("posting queue pass failed")
            try:
                await asyncio.to_thread(self.tidy_disk)
            except Exception:
                log.exception("disk cleanup failed")
            await asyncio.sleep(QUEUE_TICK_S)

    async def run_queue(self) -> None:
        """Send every queued upload whose account has a free slot (best-rated clips first), and settle the
        clips that are done."""
        async with self._queue_lock:
            await self._settle()
            clips = self.db.list_clips("queued", 500)
            uploads = self.db.uploads_for_clips([c["id"] for c in clips])
            wanting: dict[str, list[tuple[dict, str]]] = {}
            for clip in sorted(clips, key=self.rating, reverse=True):
                if time.time() - (clip["decided_at"] or 0) > self.s.queue_hours * 3600:
                    continue  # waited too long: settled once its uploads in flight are answered
                for version, account in self.remaining(clip, uploads[clip["id"]]):
                    wanting.setdefault(account, []).append((clip, version))
            for account, jobs in wanting.items():
                for clip, version in jobs:
                    if self.slot_at(account) > time.time():
                        break
                    await self._send(clip, version, account)
            await self._settle()

    async def _send(self, clip: dict, version: str, account: str) -> None:
        uid = self.db.add_upload(clip["id"], version, account)
        try:
            res = await self.poster.publish(self.version_file(clip, version), clip["caption"], [account],
                                            external_id=f"clip-{clip['id']}-{version}")
            rid = str(res.get("post_id", ""))
            self.db.update_upload(uid, request_id=rid, state="processing" if self.poster.checks(rid) else "live")
        except Exception as e:
            log.warning("uploading %s version of clip %s to %s failed: %s", version, clip["id"], account, e)
            self.db.update_upload(uid, state="limited" if is_limit(str(e)) else "failed", error=str(e)[:300])

    async def check_uploads(self) -> None:
        """Ask the provider how the uploads it accepted ended, then run the queue."""
        now = time.time()
        for u in self.db.uploads_in("processing"):
            if now - u["sent_at"] > CHECK_GIVE_UP_S:
                self.db.update_upload(u["id"], state="failed", checked_at=now,
                                      error="no answer from the network after 6 hours")
                continue
            try:
                r = await self.poster.check(u["request_id"])
            except Exception as e:
                log.warning("checking upload %s failed: %s", u["id"], e)
                continue
            self.db.update_upload(u["id"], state=r["state"], url=r.get("url") or "", error=r.get("error") or "",
                                  checked_at=now)
        await self.run_queue()

    async def _settle(self) -> None:
        """Queued clips with nothing left to send (or that waited too long) and nothing in flight get their
        final status: posted if anything went live, back to Review if it never got a slot, else failed."""
        now = time.time()
        for clip in self.db.list_clips("queued", 500):
            ups = self.db.uploads_for(clip["id"])
            left = self.remaining(clip, ups)
            expired = now - (clip["decided_at"] or now) > self.s.queue_hours * 3600
            if (left and not expired) or any(u["state"] in ("sending", "processing") for u in ups):
                continue
            await self._finish(clip, ups, left)

    async def _finish(self, clip: dict, ups: list[dict], left: list[tuple[str, str]]) -> None:
        cid = clip["id"]
        live = [u for u in ups if u["state"] == "live"]
        problems = [f"{network_of(u['account'])} {u['version']}: {u['error'] or 'failed'}"
                    for u in ups if u["state"] == "failed"]
        for version, account in left:
            turned_down = [u for u in ups if u["state"] == "limited" and (u["version"], u["account"]) == (version, account)]
            why = turned_down[-1]["error"] if turned_down else f"no free slot within {self.s.queue_hours:g}h"
            problems.append(f"{network_of(account)} {version}: not posted ({why})")
        err = "; ".join(problems)
        if live:
            self.db.update_clip(cid, status="posted", posted_at=min(u["sent_at"] for u in live),
                                post_id=",".join(u["request_id"] for u in live), post_error=err[:300])
            self.drop_clip_files(clip)  # it's up: nothing to keep locally
            where = ", ".join(f"{network_of(u['account'])} ({u['version']}) {u['url']}".strip() for u in live)
            await self.notifier.notify_posted(clip, True, (where + (f"; {err}" if err else ""))[:300])
        elif left and not any(u["state"] == "failed" for u in ups):
            self.db.update_clip(cid, status="pending", post_error=f"not posted: {err}. Approve it again to requeue"[:300])
            await self.notifier.notify_posted(clip, False, f"back in Review: {err}"[:150])
        else:
            self.db.update_clip(cid, status="failed", post_error=(err or "nothing was posted")[:300])
            await self.notifier.notify_posted(clip, False, (err or "nothing was posted")[:150])

    def reject(self, clip_id: int) -> dict:
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ClipError("clip not found")
        if not self.db.claim_clip(clip_id, ("pending", "failed"), "rejected"):
            raise ClipError(f"clip is {clip['status']}, cannot reject")
        for key in ("path", "thumb"):
            if clip.get(key):
                Path(clip[key]).unlink(missing_ok=True)
        for extra in (self.base_path(clip_id), self.full_path(clip_id), self.full_path(clip_id, base=True)):
            extra.unlink(missing_ok=True)
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
                "slots": self.posting_slots() if not self.poster.manual else [], "disk": self.disk,
                "configured": self.s.configured(), "poster": self.poster.info(),
                "category_start_ts": parse_start(self.s.category_start), "watcher": {"last_poll": self.watcher.last_poll,
                                                               "errors": self.watcher.last_error}}
