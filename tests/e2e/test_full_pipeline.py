"""The whole thing, on a synthetic stream:
 watcher sees streamer live -> recorder (fake streamlink) cuts 30 s chunks -> detector finds the loud
 moment -> clipper renders a vertical clip with pre-roll -> Discord gets live/clip/offline messages ->
 human approves through the HTTP API -> Post for Me receives the upload + post."""
import json
import subprocess

import httpx
import pytest
import respx

from app.clipper import probe_duration
from app.db import Database
from app.notifier import DiscordNotifier
from app.pipeline import Pipeline
from app.platforms.base import LiveInfo
from app.poster import PostForMe
from app.recorder import Recorder
from app.web import create_app

pytestmark = pytest.mark.e2e
PFM = "https://api.postforme.dev/v1"
HOOK = "https://discord.test/hook"


class FakePlatform:
    def __init__(self):
        self.live = True

    async def check_many(self, handles):
        return {h: (LiveInfo("twitch", h, "GTA 6 launch!", "s1", f"https://twitch.tv/{h}", 5000) if self.live else None)
                for h in handles}


def video_size(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0:s=x", str(path)], capture_output=True, text=True).stdout.strip()
    return out


@respx.mock
async def test_stream_to_clip_to_approved_post(synthetic_mp4, settings):
    discord = respx.post(HOOK).respond(204)
    respx.post(f"{PFM}/media/create-upload-url").respond(
        json={"upload_url": "https://store.example/up", "media_url": "https://cdn.example/clip.mp4"})
    upload = respx.put("https://store.example/up").respond(200)
    post = respx.post(f"{PFM}/social-posts").respond(json={"id": "sp_42"})

    fake_src = ["ffmpeg", "-v", "error", "-i", str(synthetic_mp4), "-c", "copy", "-f", "mpegts", "-"]
    http = httpx.AsyncClient()
    db = Database(":memory:")
    platform = FakePlatform()
    pl = Pipeline(settings, db, {"twitch": platform},
                  DiscordNotifier(HOOK, http, "https://clipper.example", max_upload_mb=8),
                  PostForMe("KEY", http, PFM),
                  recorder_factory=lambda info, out, s: Recorder(out, fake_src, s.chunk_seconds, s.ffmpeg_cmd),
                  chat_factory=lambda info, log: None, tick=0.2)
    db.add_streamer("twitch", "TestStreamer")

    # 1. watcher notices the streamer is live and a session starts
    await pl.poll_now()
    assert ("twitch", "teststreamer") in pl.workers
    assert pl.state()["live"][0]["handle"] == "teststreamer"

    # 2. the stream is recorded, analysed, and exactly one clip comes out
    await pl.wait_idle(timeout=180)
    clips = db.list_clips()
    assert len(clips) == 1, clips
    clip = clips[0]
    assert clip["status"] == "pending" and clip["handle"] == "teststreamer"
    assert 58 <= clip["peak_s"] <= 70                         # the burst is at 60-68 s
    assert clip["start_s"] <= clip["peak_s"] - 35 and clip["end_s"] == pytest.approx(clip["peak_s"] + 15)
    assert video_size(clip["path"]) == "1080x1920"
    assert probe_duration(clip["path"]) == pytest.approx(clip["end_s"] - clip["start_s"], abs=1.5)
    assert db.list_sessions()[0]["ended_at"] is not None
    assert (settings.data_dir / "clips" / f"{clip['id']}.jpg").exists()

    # 3. Discord was told: live -> clip (with video attached, link to review page) -> offline summary
    texts = [c.request.content for c in discord.calls]
    assert any(b"is LIVE" in t for t in texts)
    clip_msgs = [t for t in texts if b"New clip ready" in t]
    assert len(clip_msgs) == 1 and b"video/mp4" in clip_msgs[0]   # small preview attached
    assert len(clip_msgs[0]) < 8 * 1024 * 1024 + 4096
    assert not list((settings.data_dir / "clips").glob("*_preview.mp4"))  # temp preview cleaned up
    assert f"https://clipper.example/#clip-{clip['id']}".encode() in clip_msgs[0]
    assert any(b"went offline" in t and b"1 clip" in t for t in texts)

    # 4. human opens the GUI API, sees the clip, approves it
    app = create_app(pl, start_pipeline=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as gui:
        pending = (await gui.get("/api/clips", params={"status": "pending"})).json()
        assert [p["id"] for p in pending] == [clip["id"]]
        vid = await gui.get(f"/api/clips/{clip['id']}/video")
        assert vid.status_code == 200 and len(vid.content) > 10_000
        r = await gui.post(f"/api/clips/{clip['id']}/approve", json={"caption": "GTA 6 chaos", "accounts": ["spc_1", "spc_2"]})
        assert r.status_code == 200
        await pl.wait_idle()

    # 5. Post for Me got the real file bytes and the post request
    assert upload.calls[0].request.content[:4] != b"" and len(upload.calls[0].request.content) == len(vid.content)
    bodies = [json.loads(c.request.content) for c in post.calls]  # one post per account
    assert bodies == [{"caption": "GTA 6 chaos", "social_accounts": [a], "media": [{"url": "https://cdn.example/clip.mp4"}],
                       "external_id": f"clip-{clip['id']}-headline"} for a in ("spc_1", "spc_2")]
    final = db.get_clip(clip["id"])
    assert final["status"] == "posted" and final["post_id"] == "sp_42,sp_42"
    assert any(b"Posted" in c.request.content for c in discord.calls)

    # 6. the watcher re-detects the same streamer if recording ended on its own and they're still live
    assert pl.watcher.live == {}
    await pl.poll_now()
    assert ("twitch", "teststreamer") in pl.workers
    platform.live = False
    await pl.stop()
    await http.aclose()


@respx.mock
async def test_quiet_stream_makes_no_clips(tmp_path, settings):
    from tests.conftest import make_synthetic_stream
    quiet = make_synthetic_stream(tmp_path / "quiet.mp4", seconds=70, burst=(500, 501))  # no burst inside
    respx.post(HOOK).respond(204)
    http = httpx.AsyncClient()
    db = Database(":memory:")
    fake_src = ["ffmpeg", "-v", "error", "-i", str(quiet), "-c", "copy", "-f", "mpegts", "-"]
    pl = Pipeline(settings, db, {"twitch": FakePlatform()}, DiscordNotifier(HOOK, http, ""), PostForMe("", http, PFM),
                  recorder_factory=lambda i, o, s: Recorder(o, fake_src, s.chunk_seconds, s.ffmpeg_cmd),
                  chat_factory=lambda i, l: None, tick=0.2)
    db.add_streamer("twitch", "calm")
    await pl.poll_now()
    await pl.wait_idle(timeout=120)
    assert db.list_clips() == []
    assert db.list_sessions()[0]["ended_at"] is not None
    await http.aclose()


@respx.mock
async def test_auto_approve_posts_without_a_human(synthetic_mp4, settings):
    respx.post(HOOK).respond(204)
    respx.post(f"{PFM}/media/create-upload-url").respond(
        json={"upload_url": "https://store.example/up", "media_url": "https://cdn.example/a.mp4"})
    respx.put("https://store.example/up").respond(200)
    post = respx.post(f"{PFM}/social-posts").respond(json={"id": "sp_auto"})
    settings.auto_approve = True
    settings.default_accounts = "spc_7"
    fake_src = ["ffmpeg", "-v", "error", "-i", str(synthetic_mp4), "-c", "copy", "-f", "mpegts", "-"]
    http = httpx.AsyncClient()
    db = Database(":memory:")
    pl = Pipeline(settings, db, {"twitch": FakePlatform()}, DiscordNotifier(HOOK, http, ""), PostForMe("KEY", http, PFM),
                  recorder_factory=lambda i, o, s: Recorder(o, fake_src, s.chunk_seconds, s.ffmpeg_cmd),
                  chat_factory=lambda i, l: None, tick=0.2)
    db.add_streamer("twitch", "auto")
    await pl.poll_now()
    await pl.wait_idle(timeout=180)
    clip = db.list_clips()[0]
    assert clip["status"] == "posted" and clip["post_id"] == "sp_auto"
    assert json.loads(post.calls[0].request.content)["social_accounts"] == ["spc_7"]
    await http.aclose()
