import httpx
import pytest
import respx

from app.config import Settings, parse_categories, parse_start
from app.pipeline import category_watch_from
from app.platforms.base import LiveInfo
from app.platforms.kick import KickClient
from app.platforms.twitch import TwitchClient
from app.watcher import CategoryWatch, Watcher

pytestmark = pytest.mark.unit
GTA = "Grand Theft Auto VI"


def test_parse_categories_and_settings():
    assert parse_categories("Grand Theft Auto VI, kick:Fortnite\nCall of Duty: Warzone") == [
        (None, GTA), ("kick", "Fortnite"), (None, "Call of Duty: Warzone")]
    assert parse_categories("") == []
    with pytest.raises(ValueError):
        parse_categories("twitch:")
    s = Settings()
    s.update({"watch_categories": GTA, "category_streams": "4", "category_min_viewers": 500,
              "category_languages": "en, es", "category_exclude": "@Twitch", "layout": "split",
              "facecam_boxes": "caseoh_=0.7,0,0.3,0.3"})
    assert s.category_streams == 4 and s.layout == "split"
    for bad in ({"category_streams": 11}, {"layout": "sideways"}, {"facecam_boxes": "nope"},
                {"watch_categories": "kick:"}):
        with pytest.raises(ValueError):
            s.update(bad)
    assert Settings().layout == "auto"


def test_category_watch_waits_for_its_start_time():
    launch = parse_start("2026-11-18T06:00-05:00")
    assert launch == parse_start("2026-11-18 11:00Z") == 1794999600.0 and parse_start("") is None
    with pytest.raises(ValueError):
        Settings().update({"category_start": "tomorrow-ish"})
    s = Settings()
    s.update({"watch_categories": GTA, "category_start": "2026-11-18T06:00-05:00", "category_exclude": "Twitch"})
    assert category_watch_from(s, launch - 1) is None                  # countdown still running
    cw = category_watch_from(s, launch)
    assert cw.categories == [(None, GTA)] and cw.exclude == {"twitch"} and cw.streams == 3
    s.update({"category_start": ""})
    assert category_watch_from(s, 0) is not None                       # no start time: right away
    s.update({"watch_categories": ""})
    assert category_watch_from(s, launch) is None                      # feature off


@respx.mock
async def test_twitch_top_streams_by_category():
    respx.post("https://id.twitch.tv/oauth2/token").respond(json={"access_token": "T", "expires_in": 3600})
    games = respx.get("https://api.twitch.tv/helix/games").respond(json={"data": [{"id": "186", "name": GTA}]})
    streams = respx.get("https://api.twitch.tv/helix/streams").respond(json={"data": [
        {"user_login": "Big", "user_name": "Big", "game_name": GTA, "title": "t", "id": "1", "type": "live",
         "viewer_count": 9000},
        {"user_login": "small", "game_name": GTA, "id": "2", "type": "live", "viewer_count": 10}]})
    async with httpx.AsyncClient() as http:
        c = TwitchClient("cid", "sec", http)
        top = await c.top_streams(GTA, ["en"])
        await c.top_streams(GTA, ["en"])
    assert [(i.handle, i.viewers, i.game, i.name) for i in top] == [("big", 9000, GTA, "Big"), ("small", 10, GTA, "")]
    assert games.call_count == 1 and games.calls[0].request.url.params["name"] == GTA  # id cached
    q = streams.calls[0].request.url.params
    assert q["game_id"] == "186" and q.get_list("language") == ["en"] and q["first"] == "100"


@respx.mock
async def test_twitch_unknown_category_is_an_error():
    respx.post("https://id.twitch.tv/oauth2/token").respond(json={"access_token": "T", "expires_in": 3600})
    respx.get("https://api.twitch.tv/helix/games").respond(json={"data": []})
    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="no category"):
            await TwitchClient("a", "b", http).top_streams("Nope")


@respx.mock
async def test_kick_top_streams_matches_suffixed_names_and_falls_back_to_v2():
    respx.post("https://id.kick.com/oauth/token").respond(json={"access_token": "K", "expires_in": 3600})
    cats = respx.get("https://api.kick.com/public/v2/categories").respond(json={"data": [
        {"id": 1, "name": "Grand Theft Auto V (GTA)"}, {"id": 13155, "name": "Grand Theft Auto VI (GTA)"}]})
    v1 = respx.get("https://api.kick.com/public/v1/livestreams").respond(json={"data": [
        {"slug": "Small", "viewer_count": 5, "stream_title": "s", "language": "en",
         "category": {"name": "Grand Theft Auto VI (GTA)"}},
        {"slug": "big", "viewer_count": 2398, "stream_title": "b", "language": "en",
         "category": {"name": "Grand Theft Auto VI (GTA)"}}]})
    async with httpx.AsyncClient() as http:
        c = KickClient("id", "sec", http)
        top = await c.top_streams(GTA, ["en"])
        assert [(i.handle, i.viewers, i.url) for i in top] == [("big", 2398, "https://kick.com/big"),
                                                               ("small", 5, "https://kick.com/small")]
        q = v1.calls[0].request.url.params
        assert q["category_id"] == "13155" and q["sort"] == "viewer_count" and q["language"] == "en"
        assert cats.call_count == 1
        # v1 is deprecated: once it is gone, v2 is used and sorted here
        v1.respond(410)
        respx.get("https://api.kick.com/public/v2/livestreams").respond(json={"data": [
            {"channel": {"slug": "a"}, "broadcaster_user": {"username": "A"}, "title": "x", "language_code": "en",
             "viewer_count": 3, "category": {"name": "Grand Theft Auto VI (GTA)"}},
            {"channel": {"slug": "b"}, "title": "y", "language_code": "de", "viewer_count": 99},
            {"channel": {"slug": "c"}, "title": "z", "language_code": "en", "viewer_count": 50}]})
        top = await c.top_streams(GTA, ["en"])
        assert [(i.handle, i.name) for i in top] == [("c", ""), ("a", "A")]


class FakeClient:
    """Platform client whose live streams are set by the test: handle -> (viewers, game)."""

    def __init__(self, platform="twitch"):
        self.platform, self.streams = platform, {}

    def info(self, h):
        viewers, game = self.streams[h]
        return LiveInfo(self.platform, h, "t", "1", f"u/{h}", viewers, game)

    async def check_many(self, handles):
        return {h: self.info(h) if h in self.streams else None for h in handles}

    async def top_streams(self, category, languages=None):
        return sorted((self.info(h) for h, (_, g) in self.streams.items() if g.startswith(category)),
                      key=lambda i: -i.viewers)


async def test_watcher_records_the_top_streams_of_a_category():
    tw, kk = FakeClient("twitch"), FakeClient("kick")
    tw.streams = {"a": (5000, GTA), "b": (4000, GTA), "c": (2000, GTA), "tiny": (50, GTA), "other": (9e5, "Fortnite")}
    kk.streams = {"k1": (3000, GTA + " (GTA)")}
    explicit = [("twitch", "b")]
    cw = CategoryWatch([(None, GTA)], streams=3, min_viewers=100)
    events = []

    async def on_live(i):
        events.append(("live", i.platform, i.handle))

    async def on_off(i):
        events.append(("off", i.platform, i.handle))

    w = Watcher({"twitch": tw, "kick": kk}, lambda: explicit, on_live, on_off, grace_polls=1, category_watch=lambda: cw)
    await w.poll_once()
    # b is tracked by hand and does not use a slot; the biggest three others across both sites are picked
    assert w.auto == {("twitch", "a"): GTA, ("kick", "k1"): GTA, ("twitch", "c"): GTA}
    assert {e[2] for e in events} == {"a", "b", "c", "k1"} and all(e[0] == "live" for e in events)

    # a bigger stream appears, but not twice the smallest pick (c, 2000): nobody loses their slot
    events.clear()
    tw.streams["new"] = (3500, GTA)
    await w.poll_once()
    assert events == [] and ("twitch", "new") not in w.auto

    # c switches game: stopped at once (no grace polls), and its slot goes to the biggest waiting stream
    tw.streams["c"] = (2000, "Just Chatting")
    await w.poll_once()
    assert ("off", "twitch", "c") in events and ("twitch", "c") not in w.auto and ("twitch", "c") not in w.live
    events.clear()
    await w.poll_once()
    assert ("live", "twitch", "new") in events and w.auto[("twitch", "new")] == GTA

    # a huge stream shows up (launch day): it takes the smallest pick's slot (k1, 3000), one swap per poll
    tw.streams["mega"] = (90000, GTA)
    tw.streams["mega2"] = (80000, GTA)
    events.clear()
    await w.poll_once()
    assert ("off", "kick", "k1") in events and ("live", "twitch", "mega") in events
    assert ("twitch", "mega2") not in w.auto and len(w.auto) == 3
    events.clear()
    await w.poll_once()  # next poll: mega2 bumps the next smallest (new, 3500)
    assert ("off", "twitch", "new") in events and ("live", "twitch", "mega2") in events
    assert set(w.auto) == {("twitch", "a"), ("twitch", "mega"), ("twitch", "mega2")}
    del tw.streams["mega2"]
    tw.streams["new"] = (0, "Just Chatting")

    # a ends: slot freed
    del tw.streams["a"]
    events.clear()
    await w.poll_once()
    assert ("off", "twitch", "a") in events and ("twitch", "a") not in w.auto
    tw.streams["new"] = (3500, GTA)
    await w.poll_once()

    # excluded handles are dropped and never picked; turning the feature off stops every auto stream
    cw.exclude = {"new"}
    events.clear()
    await w.poll_once()
    assert ("off", "twitch", "new") in events and ("twitch", "new") not in w.auto
    cw_off = None
    w.category_watch = lambda: cw_off
    events.clear()
    await w.poll_once()
    assert w.auto == {} and ("off", "twitch", "mega") in events
    assert ("twitch", "b") in w.live  # the hand-picked streamer is untouched


async def test_category_errors_are_reported_not_raised():
    class Broken(FakeClient):
        async def top_streams(self, category, languages=None):
            raise ValueError("Twitch has no category named 'x'")

    w = Watcher({"twitch": Broken()}, lambda: [], _noop, _noop, category_watch=lambda: CategoryWatch([(None, "x")]))
    await w.poll_once()
    assert "no category" in w.last_error["twitch category"] and w.auto == {}


async def _noop(_):
    pass
