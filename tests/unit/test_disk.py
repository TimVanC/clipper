"""Keeping the disk small: leftover recordings, the recordings cap, clip files after posting / expiry."""
import os
import time

import httpx
import pytest

from app.db import Database
from app.notifier import DiscordNotifier
from app.pipeline import Pipeline, SessionWorker
from app.platforms.base import LiveInfo
from app.posters import make_poster


@pytest.fixture
def pl(settings, tmp_path):
    settings.poster_provider, settings.uploadpost_api_key = "uploadpost", "uk"
    settings.default_accounts = "default:tiktok"
    http = httpx.AsyncClient()
    return Pipeline(settings, Database(":memory:"), {}, DiscordNotifier("", http), make_poster(settings, http))


def _file(path, size, age_s=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def test_startup_sweep_deletes_leftover_recordings(pl):
    _file(pl.sessions_dir / "twitch_a" / "raw" / "chunk_00012.ts", 1000)
    _file(pl.sessions_dir / "twitch_a" / "raw" / "chunk_00013.ts", 500)
    (pl.sessions_dir / "twitch_b").mkdir()
    sid = pl.db.start_session("twitch", "a", "t", "1", str(pl.sessions_dir / "twitch_a"))
    assert pl.sweep_recordings() == 1500 and list(pl.sessions_dir.iterdir()) == []
    assert pl.db.end_open_sessions() == [sid] and pl.db.end_open_sessions() == []


def test_recordings_cap_drops_oldest_finished_chunks(pl):
    a = [_file(pl.sessions_dir / "a" / "raw" / f"chunk_{i:05d}.ts", 100, age_s=100 - i * 10) for i in range(4)]
    b = [_file(pl.sessions_dir / "b" / "raw" / f"chunk_{i:05d}.ts", 100, age_s=95 - i * 10) for i in range(2)]
    pl.s.max_buffer_gb = 350 / 1024 ** 3  # room for 3.5 chunks of the 6
    assert pl.enforce_buffer_cap() == 300
    left = sorted(p.parent.parent.name + p.name[-6:-3] for p in pl.sessions_dir.glob("*/raw/*.ts"))
    assert left == ["a002", "a003", "b001"]  # oldest first; each recorder's newest chunk is never touched
    assert pl.disk["recordings_gb"] == pytest.approx(300 / 1024 ** 3, abs=1e-6)


def _clip(pl, status, age_h=0.0, size=100):
    cid = pl.db.add_clip(1, "twitch", "x", 0, 61, 40, 6, 5, 4)
    main = _file(pl.clips_dir / f"{cid}.mp4", size)
    _file(pl.base_path(cid), size)
    _file(pl.clips_dir / f"{cid}.jpg", 10)
    pl.db.update_clip(cid, status=status, path=str(main))
    pl.db._exec("UPDATE clips SET created_at=?, decided_at=NULL WHERE id=?", (time.time() - age_h * 3600, cid))
    return cid


def test_clip_files_go_after_posting_or_expiry(pl):
    posted = _clip(pl, "posted", 1)
    old, fresh, queued = _clip(pl, "pending", 13), _clip(pl, "pending", 1), _clip(pl, "queued", 20)
    old_failed = _clip(pl, "failed", 14)
    assert pl.clean_clips() == 6 * 100
    for cid in (posted, old, old_failed):
        c = pl.db.get_clip(cid)
        assert c["path"] == "" and not pl.base_path(cid).exists() and (pl.clips_dir / f"{cid}.jpg").exists()
    assert pl.db.get_clip(posted)["status"] == "posted"
    assert pl.db.get_clip(old)["status"] == "expired" and "12h" in pl.db.get_clip(old)["post_error"]
    assert pl.db.get_clip(old_failed)["status"] == "expired"
    for cid in (fresh, queued):
        assert pl.db.get_clip(cid)["path"] and pl.base_path(cid).exists()
    # over the size cap: the oldest unposted clip goes first, a queued one never
    newer = _clip(pl, "pending", 0.5)
    pl.s.max_clips_gb = 500 / 1024 ** 3  # 660 bytes on disk: one clip (200) has to go
    pl.clean_clips()
    assert pl.db.get_clip(fresh)["status"] == "expired" and pl.db.get_clip(newer)["status"] == "pending"
    assert pl.db.get_clip(queued)["status"] == "queued"
    gone = _clip(pl, "pending", 0.1)
    (pl.clips_dir / f"{gone}.mp4").unlink()  # deleted outside the app
    pl.clean_clips()
    assert pl.db.get_clip(gone)["status"] == "expired" and "missing" in pl.db.get_clip(gone)["post_error"]


def test_full_frame_only_when_a_network_posts_it(pl):
    s = pl.s
    s.auto_post_version, s.tiktok_version, s.youtube_version = "all", "headline", "headline"
    assert not pl.wants_full()
    s.youtube_version = "all"
    assert pl.wants_full()
    s.auto_post_version = "headline"
    assert not pl.wants_full()
    s.auto_post_version = "full"
    assert pl.wants_full()
    s.full_frame_version = False
    assert not pl.wants_full()


class _Rec:
    def __init__(self, raw):
        self.raw, self.finished = raw, False

    def completed_chunks(self):
        return sorted(self.raw.glob("chunk_*.ts"))


async def test_chunks_are_tracked_by_name_not_position(pl, tmp_path):
    """Deleting old chunks used to shift positions, so a new chunk was skipped after every deletion."""
    w = SessionWorker(pl, LiveInfo("twitch", "x"), 1, tmp_path / "s")
    raw = tmp_path / "s" / "raw"
    w.recorder = _Rec(raw)
    done = []

    async def fake(path):
        done.append(path.name)
    w._process_chunk = fake
    for i in range(3):
        _file(raw / f"chunk_{i:05d}.ts", 10)
    await w._drain(final=False)
    (raw / "chunk_00000.ts").unlink()  # the rolling buffer drops the oldest
    _file(raw / "chunk_00003.ts", 10)
    await w._drain(final=False)
    assert done == ["chunk_00000.ts", "chunk_00001.ts", "chunk_00002.ts", "chunk_00003.ts"]
