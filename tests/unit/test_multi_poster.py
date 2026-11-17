import httpx
import pytest
import respx

from app.config import Settings
from app.db import Database
from app.doctor import run as doctor_run
from app.notifier import DiscordNotifier
from app.pipeline import Pipeline
from app.posters import MultiPoster, PosterError, Postproxy, UploadPost, make_poster

pytestmark = pytest.mark.unit
UP = "https://api.upload-post.com"
PP = "https://api.postproxy.dev"
ENV = {"POSTER_PROVIDER": "postproxy, UploadPost", "POSTPROXY_API_KEY": "pk", "UPLOADPOST_API_KEY": "uk"}


@pytest.fixture
def clip_file(tmp_path):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"VIDEOBYTES")
    return f


def mock_lists():
    respx.get(f"{PP}/api/profiles").respond(json={"data": [
        {"id": "AnUZrZ", "username": "clips", "platform": "tiktok", "status": "active"}]})
    respx.get(f"{UP}/api/uploadposts/users").respond(json={"success": True, "profiles": [
        {"username": "default", "social_accounts": {"youtube": {"handle": "@yt"}}}]})


def test_config_accepts_several_providers():
    s = Settings.from_env(ENV)
    assert s.poster_provider == "postproxy,uploadpost" and s.poster_names() == ["postproxy", "uploadpost"]
    assert s.configured()["postproxy"] is True and s.configured()["uploadpost"] is True
    assert "postforme" not in s.configured()
    with pytest.raises(ValueError, match="cannot be combined"):
        Settings.from_env({"POSTER_PROVIDER": "none,postproxy"})
    with pytest.raises(ValueError, match="POSTER_PROVIDER"):
        Settings.from_env({"POSTER_PROVIDER": "postproxy,buffer"})


@respx.mock
async def test_multi_lists_accounts_with_provider_prefix():
    mock_lists()
    async with httpx.AsyncClient() as http:
        p = make_poster(Settings.from_env(ENV), http)
        assert isinstance(p, MultiPoster) and p.enabled and p.label == "Postproxy + Upload-Post"
        ids = [a["id"] for a in await p.list_accounts()]
        assert ids == ["postproxy/AnUZrZ", "uploadpost/default:youtube"]
        # one provider down: the other's accounts are still listed
        respx.get(f"{UP}/api/uploadposts/users").respond(500, text="boom")
        assert [a["id"] for a in await p.list_accounts()] == ["postproxy/AnUZrZ"]
        respx.get(f"{PP}/api/profiles").respond(401, json={"error": "Invalid API key"})
        with pytest.raises(PosterError, match="Postproxy.*Upload-Post"):
            await p.list_accounts()


@respx.mock
async def test_multi_publish_routes_accounts_to_their_provider(clip_file):
    pp = respx.post(f"{PP}/api/posts").respond(201, json={"id": "xyz", "platforms": []})
    up = respx.post(f"{UP}/api/upload").respond(json={"success": True, "request_id": "req_1"})
    async with httpx.AsyncClient() as http:
        p = MultiPoster([Postproxy("pk", http), UploadPost("uk", http)])
        res = await p.publish(clip_file, "cap", ["postproxy/AnUZrZ", "uploadpost/default:youtube"], "clip-1")
        assert res["post_id"] == "postproxy:xyz,uploadpost:req_1" and "partial_error" not in res
        assert pp.call_count == 1 and up.call_count == 1
        assert b'name="profiles[]"\r\n\r\nAnUZrZ' in pp.calls[0].request.content
        assert b'name="user"\r\n\r\ndefault' in up.calls[0].request.content
        assert pp.calls[0].request.headers["Authorization"] == "Bearer pk"
        assert up.calls[0].request.headers["Authorization"] == "Apikey uk"
        # only one provider's accounts selected -> the other is not called
        await p.publish(clip_file, "cap", ["postproxy/AnUZrZ"])
        assert pp.call_count == 2 and up.call_count == 1
        for bad in (["AnUZrZ"], ["postforme/x"], ["postproxy/"]):
            with pytest.raises(PosterError, match="bad account id"):
                await p.publish(clip_file, "cap", bad)
        with pytest.raises(PosterError, match="no social accounts"):
            await p.publish(clip_file, "cap", [])
    assert pp.call_count == 2 and up.call_count == 1


@respx.mock
async def test_multi_publish_partial_and_total_failure(clip_file):
    respx.post(f"{PP}/api/posts").respond(201, json={"id": "xyz", "platforms": []})
    respx.post(f"{UP}/api/upload").respond(403, json={"success": False, "message": "plan limit"})
    async with httpx.AsyncClient() as http:
        p = MultiPoster([Postproxy("pk", http), UploadPost("", http)])
        res = await p.publish(clip_file, "cap", ["postproxy/a", "uploadpost/default:youtube"])
        assert res["post_id"] == "postproxy:xyz"
        assert "posted via Postproxy" in res["partial_error"] and "UPLOADPOST_API_KEY" in res["partial_error"]
        p = MultiPoster([Postproxy("pk", http), UploadPost("uk", http)])
        res = await p.publish(clip_file, "cap", ["postproxy/a", "uploadpost/default:youtube"])
        assert "Upload-Post" in res["partial_error"] and "plan limit" in res["partial_error"]
        respx.post(f"{PP}/api/posts").respond(422, json={"errors": ["nope"]})
        with pytest.raises(PosterError, match="Postproxy.*nope.*Upload-Post.*plan limit"):
            await p.publish(clip_file, "cap", ["postproxy/a", "uploadpost/default:youtube"])


@respx.mock
async def test_pipeline_partial_failure_is_posted_with_error_and_not_retryable(settings, tmp_path):
    respx.post(f"{PP}/api/posts").respond(201, json={"id": "xyz", "platforms": []})
    up = respx.post(f"{UP}/api/upload").respond(500, text="boom")
    settings.poster_provider = "postproxy,uploadpost"
    settings.postproxy_api_key, settings.uploadpost_api_key = "pk", "uk"
    settings.default_accounts = "postproxy/a,uploadpost/default:youtube"
    http = httpx.AsyncClient()
    db = Database(":memory:")
    pl = Pipeline(settings, db, {}, DiscordNotifier("", http), make_poster(settings, http))
    video = tmp_path / "clip1.mp4"
    video.write_bytes(b"0" * 100)
    cid = db.add_clip(db.start_session("twitch", "speed", "t", "1", str(tmp_path)), "twitch", "speed",
                      10, 80, 60, 5.5, 5.0, 3.0)
    db.update_clip(cid, status="pending", path=str(video), thumb="", caption="cap")
    assert (await pl.approve(cid))["status"] == "posting"
    await pl.wait_idle()
    clip = db.get_clip(cid)
    assert clip["status"] == "posted" and clip["post_id"] == "postproxy:xyz"
    assert "Upload-Post" in clip["post_error"] and "500" in clip["post_error"]
    with pytest.raises(ValueError, match="posted"):  # no retry -> no double post on Postproxy
        await pl.approve(cid)
    assert up.call_count == 1
    await http.aclose()


@respx.mock
async def test_doctor_checks_each_combined_provider(capsys):
    mock_lists()
    assert await doctor_run(Settings.from_env(ENV)) in (0, 1)  # binaries may be missing on the test box
    out = capsys.readouterr().out
    assert "tiktok:clips(postproxy/AnUZrZ)" in out and "(uploadpost/default:youtube)" in out
    await doctor_run(Settings.from_env({**ENV, "UPLOADPOST_API_KEY": ""}))
    assert "[FAIL] Poster: Upload-Post" in capsys.readouterr().out
