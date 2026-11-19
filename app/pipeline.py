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
from . import headline
from .facecam import choose_layout
from .db import Database
from .detector import DetectorConfig, HypeDetector, audio_db_per_second, decode_audio, SR
from .notifier import DiscordNotifier
from .platforms.base import LiveInfo, PlatformClient
from .posters import Poster
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

# posts per connected account per rolling 24h that Upload-Post lets through (docs.upload-post.com/guides/limit-of-uploads)
NETWORK_CAPS = {"tiktok": 15, "youtube": 30, "instagram": 50, "facebook": 25, "threads": 50, "linkedin": 150,
                "pinterest": 20, "x": 10, "bluesky": 25}


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
        if not self.s.full_frame_version:
            return
        full = self.full_path(clip_id)
        try:
            await asyncio.to_thread(render_clip, pieces, full, "classic", self.s.ffmpeg_cmd)
        except Exception:
            log.exception("full-frame version failed for clip %s", clip_id)
            full.unlink(missing_ok=True)

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

    def same_setup(self, accounts: str) -> bool:
        """Was a clip posted through the current posting provider(s)? The daily cap mirrors the provider's own
        limit (Upload-Post: 15 TikTok posts a day), so posts made through a provider no longer used don't count.
        One provider: account ids have no 'provider/' prefix; several: every prefix is a current provider."""
        names = self.s.poster_names()
        ids = [a for a in (accounts or "").split(",") if a]
        if len(names) == 1:
            return all("/" not in a for a in ids)
        return all(a.partition("/")[0] in names for a in ids)

    def cap_for(self, account: str) -> int:
        return min(NETWORK_CAPS.get(network_of(account), self.s.max_posts_per_day), self.s.max_posts_per_day)

    def uploads_today(self) -> dict[str, int]:
        """Uploads per account in the last 24h through the current setup. post_id holds one entry per upload:
        'id' or 'version=id', with '@acct+acct' when it went to only some of the clip's accounts."""
        used: dict[str, int] = {}
        for r in self.db.posts_since(time.time() - 86400):
            if not self.same_setup(r["accounts"]):
                continue
            accts = [a for a in (r["accounts"] or "").split(",") if a] or ["*"]  # no accounts yet: counts for all
            for entry in (r["post_id"] or "").split(",") if r["post_id"] else [""]:
                targets = entry.rsplit("@", 1)[1].split("+") if "@" in entry else accts
                for a in targets:
                    used[a] = used.get(a, 0) + 1
        return used

    def post_plan(self, clip: dict, version: str, accounts: list[str]) -> tuple[list[tuple[str, Path, list[str]]],
                                                                               list[str]]:
        """([(version, file, accounts)], [accounts skipped: daily cap]). Posting all versions, TikTok gets only
        tiktok_version (unless that is 'all'); an account at its daily cap is left out, the others still post."""
        files = self.posting_files(clip, version)
        names = [n for n, _ in files]
        tiktok_pick = self.s.tiktok_version if self.s.tiktok_version in names else names[0]
        used = self.uploads_today()
        plan, capped = [], []
        for name, video in files:
            targets = []
            for a in accounts:
                if (version == "all" and network_of(a) == "tiktok" and self.s.tiktok_version != "all"
                        and name != tiktok_pick):
                    continue
                if used.get(a, 0) + used.get("*", 0) >= self.cap_for(a):
                    if a not in capped:
                        capped.append(a)
                    continue
                used[a] = used.get(a, 0) + 1
                targets.append(a)
            if targets:
                plan.append((name, video, targets))
        return plan, capped

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
        plan, capped = self.post_plan(clip, version, accts)
        if not plan:
            raise ClipError(f"daily post cap reached for {', '.join(network_of(a) for a in capped)}")
        if not self.db.claim_clip(clip_id, ("pending", "failed"), "posting"):
            raise ClipError(f"clip is {clip['status']}, cannot approve")
        cap = caption if caption is not None and caption.strip() else (clip["caption"] or "")
        self.db.update_clip(clip_id, caption=cap, accounts=",".join(accts), post_error="")
        self._spawn(self._do_post(clip_id, plan, capped, named=version == "all"))
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

    async def _do_post(self, clip_id: int, plan: list[tuple[str, Path, list[str]]], capped: list[str],
                       named: bool = False) -> None:
        """Post each planned version as its own post. Any success marks the clip posted (so a retry can't post
        the ones that went out twice); failures and accounts left out (daily cap) are listed in post_error."""
        clip = self.db.get_clip(clip_id)
        everyone = clip["accounts"].split(",")
        ids = []
        errors = [f"{', '.join(network_of(a) for a in capped)}: daily cap reached, not posted there"] if capped else []
        for name, video, targets in plan:
            try:
                res = await self.poster.publish(video, clip["caption"], targets,
                                                external_id=f"clip-{clip_id}-{name}" if named else f"clip-{clip_id}")
                entry = f"{name}={res['post_id']}" if named else res["post_id"]
                ids.append(entry + ("" if targets == everyone else "@" + "+".join(targets)))
                if res.get("partial_error"):  # combined providers: some posted, some failed
                    errors.append(f"{name}: {res['partial_error']}")
            except Exception as e:
                log.exception("posting %s version of clip %s failed", name, clip_id)
                errors.append(f"{name}: {e}" if named else str(e))
        if not ids:
            self.db.update_clip(clip_id, status="failed", post_error="; ".join(errors)[:300])
            await self.notifier.notify_posted(clip, False, "; ".join(errors)[:150])
            return
        self.db.update_clip(clip_id, status="posted", post_id=",".join(ids), posted_at=time.time(),
                            post_error="; ".join(errors)[:300])
        where = ", ".join(f"{name} -> {'+'.join(network_of(a) for a in t)}" for name, _, t in plan)
        await self.notifier.notify_posted(clip, not errors, (where + (f"; {'; '.join(errors)}" if errors else ""))[:300])

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
                "configured": self.s.configured(), "poster": self.poster.info(),
                "category_start_ts": parse_start(self.s.category_start), "watcher": {"last_poll": self.watcher.last_poll,
                                                               "errors": self.watcher.last_error}}
