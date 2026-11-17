import httpx
import pytest
import respx

from app.platforms.base import LiveInfo
from app.platforms.kick import KickClient
from app.platforms.twitch import TwitchClient
from app.platforms.youtube import YouTubeClient
from app.watcher import Watcher

pytestmark = pytest.mark.unit


@respx.mock
async def test_twitch_live_and_offline_and_token_reuse():
    tok = respx.post("https://id.twitch.tv/oauth2/token").respond(json={"access_token": "T", "expires_in": 3600})
    streams = respx.get("https://api.twitch.tv/helix/streams").respond(json={"data": [
        {"user_login": "SpeedRuns", "id": "42", "title": "ranked", "viewer_count": 1234, "type": "live"}]})
    async with httpx.AsyncClient() as http:
        c = TwitchClient("cid", "sec", http)
        res = await c.check_many(["speedruns", "nobody"])
        assert res["nobody"] is None
        info = res["speedruns"]
        assert info == LiveInfo("twitch", "speedruns", "ranked", "42", "https://www.twitch.tv/speedruns", 1234)
        await c.check_many(["speedruns"])
    assert tok.call_count == 1  # token cached
    req = streams.calls[0].request
    assert req.headers["Authorization"] == "Bearer T" and req.headers["Client-Id"] == "cid"
    assert req.url.params.get_list("user_login") == ["speedruns", "nobody"]


@respx.mock
async def test_twitch_refreshes_token_on_401():
    respx.post("https://id.twitch.tv/oauth2/token").respond(json={"access_token": "T", "expires_in": 3600})
    respx.get("https://api.twitch.tv/helix/streams").mock(side_effect=[
        httpx.Response(401), httpx.Response(200, json={"data": []})])
    async with httpx.AsyncClient() as http:
        assert await TwitchClient("a", "b", http).check_many(["x"]) == {"x": None}


@respx.mock
async def test_kick_parses_live_channels():
    respx.post("https://id.kick.com/oauth/token").respond(json={"access_token": "K", "expires_in": 3600})
    respx.get("https://api.kick.com/public/v1/channels").respond(json={"data": [
        {"slug": "adinross", "stream_title": "gta", "stream": {"is_live": True, "viewer_count": 9, "start_time": "t0"}},
        {"slug": "other", "stream": {"is_live": False}}]})
    async with httpx.AsyncClient() as http:
        res = await KickClient("a", "b", http).check_many(["adinross", "other", "ghost"])
    assert res["adinross"].url == "https://kick.com/adinross" and res["adinross"].viewers == 9
    assert res["other"] is None and res["ghost"] is None


@respx.mock
async def test_youtube_finds_live_video_via_uploads_playlist():
    respx.get("https://www.googleapis.com/youtube/v3/channels").respond(json={"items": [{"id": "UCabcdefghijklmnopqrstuv"}]})
    pl = respx.get("https://www.googleapis.com/youtube/v3/playlistItems").respond(json={"items": [
        {"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}]})
    respx.get("https://www.googleapis.com/youtube/v3/videos").respond(json={"items": [
        {"id": "v1", "snippet": {"liveBroadcastContent": "none", "title": "old"}},
        {"id": "v2", "snippet": {"liveBroadcastContent": "live", "title": "LIVE now"},
         "liveStreamingDetails": {"concurrentViewers": "77"}}]})
    async with httpx.AsyncClient() as http:
        res = await YouTubeClient("KEY", http).check_many(["mrbeast"])
    info = res["mrbeast"]
    assert info.stream_id == "v2" and info.url.endswith("v=v2") and info.viewers == 77
    assert pl.calls[0].request.url.params["playlistId"] == "UUabcdefghijklmnopqrstuv"


@respx.mock
async def test_youtube_error_leaves_handle_unknown():
    respx.get("https://www.googleapis.com/youtube/v3/channels").respond(403)
    async with httpx.AsyncClient() as http:
        assert await YouTubeClient("KEY", http).check_many(["x"]) == {}


class FakeClient:
    def __init__(self):
        self.script = []  # list of dict-or-Exception

    async def check_many(self, handles):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def info(h="a"):
    return LiveInfo("twitch", h, "t", "1", f"https://twitch.tv/{h}")


async def test_watcher_transitions_grace_and_error_handling():
    events, fc = [], FakeClient()

    async def on_live(i): events.append(("live", i.handle))

    async def on_off(i): events.append(("off", i.handle))

    w = Watcher({"twitch": fc}, lambda: [("twitch", "a")], on_live, on_off, grace_polls=2)
    fc.script = [{"a": info()}, {"a": info()}, RuntimeError("api down"), {"a": None}, {"a": info()},
                 {"a": None}, {"a": None}, {}]
    for _ in range(8):
        await w.poll_once()
    # live once; API error and a single miss do not end it; two consecutive misses do; empty result = unknown
    assert events == [("live", "a"), ("off", "a")]
    assert w.live == {}


async def test_watcher_ignores_unconfigured_platform_and_stops_removed_streamers():
    events, fc = [], FakeClient()
    streamers = [("twitch", "a"), ("kick", "b")]

    async def on_live(i): events.append(("live", i.handle))

    async def on_off(i): events.append(("off", i.handle))

    w = Watcher({"twitch": fc}, lambda: list(streamers), on_live, on_off, grace_polls=1)
    fc.script = [{"a": info()}, {}]
    await w.poll_once()
    assert w.last_error["kick"] == "not configured"
    streamers.remove(("twitch", "a"))  # user deletes the streamer while live
    await w.poll_once()
    assert events == [("live", "a"), ("off", "a")]
