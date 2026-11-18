import httpx
import pytest
import respx

from app.captions import DEFAULT_HOOKS, build_caption, build_hashtags, display_name, for_platform, tag
from app.config import Settings
from app.platforms.twitch import TwitchClient
from app.posters.postproxy import Postproxy
from app.posters.uploadpost import UploadPost

UP = "https://api.upload-post.com"
PP = "https://api.postproxy.dev"


@pytest.fixture
def clip_file(tmp_path):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"VIDEOBYTES")
    return f


def test_tag_and_display_name():
    assert tag("Just Chatting") == "justchatting" and tag("caseoh_") == "caseoh" and tag("!!") == ""
    assert display_name("caseoh_", "CaseOh_") == "CaseOh"
    assert display_name("n3on") == "N3on" and display_name("kaicenat", "KaiCenat") == "KaiCenat"


def test_hashtags_go_from_specific_to_broad_and_respect_the_limit():
    tags = build_hashtags("caseoh_", "twitch", "Fortnite", "#clips, #streamer #Funny", limit=5)
    assert tags == ["caseoh", "fortnite", "caseohclips", "twitch", "clips"]
    assert build_hashtags("kaicenat", "twitch", "Grand Theft Auto V", "", 9) == ["kaicenat", "gta", "kaicenatclips",
                                                                                 "twitch"]
    # network tags are never part of the stored caption: for_platform adds the right one when posting
    assert "fyp" not in build_hashtags("x", "kick", "", "#fyp #shorts #clips", 9)
    assert build_hashtags("x", "kick", "", "#clips #clips #x", 9) == ["x", "xclips", "kick", "clips"]


def test_build_caption_rotates_hooks_and_never_puts_underscores_in_tags():
    a = build_caption(handle="caseoh_", name="CaseOh_", platform="twitch", seed=0, extra_tags="#clips")
    b = build_caption(handle="caseoh_", name="CaseOh_", platform="twitch", seed=1, extra_tags="#clips")
    assert a.splitlines()[0] == DEFAULT_HOOKS[0].format(name="CaseOh") and a.splitlines()[0] != b.splitlines()[0]
    assert a.splitlines()[-1] == "#caseoh #caseohclips #twitch #clips"
    # game hooks join the rotation only in a real game category
    pool = {build_caption(handle="x", game="Fortnite", seed=i).splitlines()[0] for i in range(10)}
    assert any("Fortnite" in h for h in pool)
    gta = {build_caption(handle="x", game="Grand Theft Auto VI (GTA)", seed=i) for i in range(10)}
    assert any("GTA 6" in c.splitlines()[0] for c in gta) and not any("Grand Theft" in c for c in gta)
    assert all("#gta6" in c for c in gta)
    assert not any("Just Chatting" in build_caption(handle="x", game="Just Chatting", seed=i).splitlines()[0]
                   for i in range(10))


def test_build_caption_custom_template_and_hooks():
    c = build_caption("{hook} | {title} | {game}\\n{hashtags}", handle="n3on", platform="kick", title="LA IRL",
                      game="IRL", seed=3, hooks="{name} is wild\n\n{name} again", max_tags=2)
    assert c == "N3on again | LA IRL | IRL\n#n3on #irl"
    # a broken template or hook falls back to the built-in ones instead of crashing the render
    assert build_caption("{nope}", handle="x", seed=0, hooks="{bad}").startswith(DEFAULT_HOOKS[0].format(name="X"))


def test_settings_validate_caption_fields():
    s = Settings()
    s.update({"caption_template": "{hook} {name} {game} {title} {streamer} {platform}\\n{hashtags}",
              "caption_hooks": "{name} did it\n{game} moment", "hashtags": "#a #b", "max_hashtags": "3"})
    assert s.max_hashtags == 3 and s.caption_hooks.count("\n") == 1
    for bad in ({"caption_template": "{unknown}"}, {"caption_hooks": "{hashtags}"}, {"max_hashtags": 0}):
        with pytest.raises(ValueError):
            s.update(bad)


def test_for_platform_adds_the_network_tag_and_drops_foreign_ones():
    cap = "CaseOh lost it \U0001F525\n\n#caseoh #clips #fyp"
    tt, yt = for_platform(cap, "tiktok"), for_platform(cap, "youtube")
    assert tt.text == cap and tt.tags == ["caseoh", "clips", "fyp"]
    assert yt.text == "CaseOh lost it \U0001F525\n\n#caseoh #clips #shorts"
    assert yt.title == "CaseOh lost it \U0001F525 #shorts"
    assert for_platform("no tags here", "tiktok").text == "no tags here\n\n#fyp"
    assert for_platform(cap, None).text == cap and for_platform(cap, "linkedin").text == cap
    assert for_platform("We are #1 today", "youtube").title == "We are #1 today #shorts"


def test_for_platform_limits():
    yt = for_platform("A" * 150 + "\n\n#x", "youtube")
    assert len(yt.title) == 100 and yt.title.endswith("… #shorts")
    x = for_platform("B" * 260 + " #one #two #three #four", "twitter")
    assert len(x.text) <= 280 and x.text.startswith("B" * 260 + " #one") and "#four" not in x.text


@respx.mock
async def test_twitch_live_info_carries_game_and_display_name():
    respx.post("https://id.twitch.tv/oauth2/token").respond(json={"access_token": "t", "expires_in": 3600})
    respx.get("https://api.twitch.tv/helix/streams").respond(json={"data": [
        {"user_login": "caseoh_", "user_name": "CaseOh_", "game_name": "Fortnite", "title": "hi", "id": "1",
         "type": "live", "viewer_count": 5}]})
    async with httpx.AsyncClient() as http:
        info = (await TwitchClient("id", "secret", http).check_many(["caseoh_"]))["caseoh_"]
    assert info.game == "Fortnite" and info.name == "CaseOh_"


@respx.mock
async def test_uploadpost_sends_a_caption_per_network(clip_file):
    route = respx.post(f"{UP}/api/upload").respond(json={"success": True, "request_id": "r1"})
    cap = "CaseOh lost it\n\n#caseoh #clips"
    async with httpx.AsyncClient() as http:
        await UploadPost("KEY", http).publish(clip_file, cap, ["main:youtube", "main:tiktok"])
    body = route.calls[0].request.content.decode()
    assert 'name="youtube_title"\r\n\r\nCaseOh lost it #shorts\r\n' in body
    assert 'name="youtube_description"\r\n\r\nCaseOh lost it\n\n#caseoh #clips #shorts\r\n' in body
    assert 'name="tiktok_title"\r\n\r\nCaseOh lost it\n\n#caseoh #clips #fyp\r\n' in body
    assert body.count('name="tags[]"') == 2 and 'name="tags[]"\r\n\r\nshorts' not in body
    assert f'name="title"\r\n\r\n{cap}\r\n' in body  # the generic fallback stays untouched


@respx.mock
async def test_postproxy_posts_once_per_network_with_its_own_caption(clip_file):
    respx.get(f"{PP}/api/profiles").respond(json={"data": [
        {"id": "tt1", "platform": "tiktok", "username": "a", "status": "active"},
        {"id": "tt2", "platform": "tiktok", "username": "b", "status": "active"},
        {"id": "yt1", "platform": "youtube", "username": "c", "status": "active"}]})
    route = respx.post(f"{PP}/api/posts").mock(side_effect=[
        httpx.Response(201, json={"id": "p1", "platforms": []}), httpx.Response(201, json={"id": "p2", "platforms": []})])
    async with httpx.AsyncClient() as http:
        p = Postproxy("KEY", http)
        res = await p.publish(clip_file, "hi\n\n#clips", ["tt1", "yt1", "tt2"])
        assert res["post_id"] == "p1,p2" and "partial_error" not in res
        first, second = (c.request.content.decode() for c in route.calls)
        assert 'name="post[body]"\r\n\r\nhi\n\n#clips #fyp\r\n' in first and first.count('name="profiles[]"') == 2
        assert 'name="post[body]"\r\n\r\nhi\n\n#clips #shorts\r\n' in second and second.count('name="profiles[]"') == 1
        # one network failing after another went out is reported, not raised (a retry would double-post)
        route.mock(side_effect=[httpx.Response(201, json={"id": "p3", "platforms": []}),
                                httpx.Response(422, json={"error": "nope"})])
        res = await p.publish(clip_file, "hi", ["tt1", "yt1"])
        assert res["post_id"] == "p3" and "nope" in res["partial_error"]


@respx.mock
async def test_postproxy_still_posts_when_the_profile_lookup_fails(clip_file):
    respx.get(f"{PP}/api/profiles").respond(500)
    route = respx.post(f"{PP}/api/posts").respond(201, json={"id": "p1", "platforms": []})
    async with httpx.AsyncClient() as http:
        res = await Postproxy("KEY", http).publish(clip_file, "hi #clips", ["tt1"])
    assert res["post_id"] == "p1" and b'name="post[body]"\r\n\r\nhi #clips\r\n' in route.calls[0].request.content
