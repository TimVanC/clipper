import httpx
import pytest
import respx

from app.config import Settings
from app.db import Database
from app.doctor import run as doctor_run
from app.notifier import DiscordNotifier
from app.pipeline import ClipError, Pipeline
from app.posters import (ManualPoster, PostForMe, Postproxy, PostproxyError, UploadPost, UploadPostError,
                         make_poster)
from app.web import create_app

pytestmark = pytest.mark.unit
UP = "https://api.upload-post.com"
PP = "https://api.postproxy.dev"


@pytest.fixture
def clip_file(tmp_path):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"VIDEOBYTES")
    return f


# ---- Upload-Post -------------------------------------------------
@respx.mock
async def test_uploadpost_list_accounts_flattens_profiles():
    route = respx.get(f"{UP}/api/uploadposts/users").respond(json={"success": True, "profiles": [
        {"username": "main", "social_accounts": {
            "tiktok": {"handle": "clipz", "display_name": "Clipz", "reauth_required": False},
            "youtube": {"display_name": "Clipz TV", "reauth_required": True},
            "instagram": "", "x": None}}]})
    async with httpx.AsyncClient() as http:
        accts = await UploadPost("KEY", http).list_accounts()
    assert route.calls[0].request.headers["Authorization"] == "Apikey KEY"
    assert accts == [
        {"id": "main:tiktok", "platform": "tiktok", "username": "clipz (main)", "status": "connected"},
        {"id": "main:youtube", "platform": "youtube", "username": "Clipz TV (main)", "status": "reauth_required"}]


@respx.mock
async def test_uploadpost_publish_one_multipart_upload_per_profile(clip_file):
    route = respx.post(f"{UP}/api/upload").respond(json={"success": True, "request_id": "req_1", "total_platforms": 2})
    async with httpx.AsyncClient() as http:
        res = await UploadPost("KEY", http).publish(clip_file, "hello #fyp", ["main:tiktok", "main:youtube"],
                                                    external_id="clip-3")
    assert res["post_id"] == "req_1" and route.call_count == 1
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Apikey KEY"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert body.count(b'name="platform[]"') == 2 and b"tiktok" in body and b"youtube" in body
    for part in (b'name="user"\r\n\r\nmain', b'name="title"\r\n\r\nhello #fyp', b'name="youtube_title"',
                 b'name="async_upload"\r\n\r\ntrue', b'name="external_id"\r\n\r\nclip-3',
                 b'name="video"; filename="c.mp4"', b"VIDEOBYTES"):
        assert part in body


@respx.mock
async def test_uploadpost_errors(clip_file):
    async with httpx.AsyncClient() as http:
        p = UploadPost("KEY", http)
        respx.post(f"{UP}/api/upload").respond(401, json={"success": False, "message": "Invalid API key"})
        with pytest.raises(UploadPostError, match="401: Invalid API key"):
            await p.publish(clip_file, "c", ["main:tiktok"])
        respx.clear()
        respx.post(f"{UP}/api/upload").respond(json={"success": True, "results": {
            "tiktok": {"success": True, "url": "https://t"}, "youtube": {"success": False, "error": "quota"}}})
        with pytest.raises(UploadPostError, match="youtube: quota"):  # sync answer, one platform failed
            await p.publish(clip_file, "c", ["main:tiktok", "main:youtube"])
        with pytest.raises(UploadPostError, match="bad account id"):
            await p.publish(clip_file, "c", ["tiktok"])
        with pytest.raises(UploadPostError, match="no social accounts"):
            await p.publish(clip_file, "c", [])


# ---- Postproxy -------------------------------------------------
@respx.mock
async def test_postproxy_list_accounts():
    route = respx.get(f"{PP}/api/profiles").respond(json={"data": [
        {"id": "prof1", "name": "My Page", "username": None, "platform": "facebook", "status": "active"},
        {"id": "prof2", "name": "Clipz", "username": "clipz", "platform": "tiktok", "status": "expired"}]})
    async with httpx.AsyncClient() as http:
        accts = await Postproxy("KEY", http).list_accounts()
    assert route.calls[0].request.headers["Authorization"] == "Bearer KEY"
    assert accts == [{"id": "prof1", "platform": "facebook", "username": "My Page", "status": "active"},
                     {"id": "prof2", "platform": "tiktok", "username": "clipz", "status": "expired"}]


@respx.mock
async def test_postproxy_publish_multipart(clip_file):
    route = respx.post(f"{PP}/api/posts").respond(201, json={"id": "xyz789", "status": "processed", "platforms": [
        {"platform": "tiktok", "profile_id": "prof2", "status": "pending", "error": None}]})
    async with httpx.AsyncClient() as http:
        res = await Postproxy("KEY", http).publish(clip_file, "hello", ["prof1", "prof2"], external_id="clip-3")
    assert res["post_id"] == "xyz789"
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer KEY"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert b'name="post[body]"\r\n\r\nhello' in body and body.count(b'name="profiles[]"') == 2
    assert b'name="media[]"; filename="c.mp4"' in body and b"VIDEOBYTES" in body


@respx.mock
async def test_postproxy_errors(clip_file):
    async with httpx.AsyncClient() as http:
        p = Postproxy("KEY", http)
        respx.post(f"{PP}/api/posts").respond(422, json={"errors": ["Media is required for tiktok"]})
        with pytest.raises(PostproxyError, match="422: Media is required"):
            await p.publish(clip_file, "c", ["prof2"])
        respx.clear()
        respx.get(f"{PP}/api/profiles").respond(401, json={"error": "Invalid API key"})
        with pytest.raises(PostproxyError, match="401: Invalid API key"):
            await p.list_accounts()
        with pytest.raises(PostproxyError, match="no social accounts"):
            await p.publish(clip_file, "c", [])


# ---- provider selection -------------------------------------------------
async def test_provider_selection_from_env():
    async with httpx.AsyncClient() as http:
        s = Settings.from_env({"POSTER_PROVIDER": "UploadPost", "UPLOADPOST_API_KEY": "u"})
        p = make_poster(s, http)
        assert isinstance(p, UploadPost) and p.key == "u" and s.configured()["uploadpost"] is True
        s = Settings.from_env({"POSTER_PROVIDER": "postproxy"})
        p = make_poster(s, http)
        assert isinstance(p, Postproxy) and not p.enabled and s.configured()["postproxy"] is False
        s = Settings.from_env({"POSTFORME_API_KEY": "k"})  # default stays Post for Me
        assert isinstance(make_poster(s, http), PostForMe) and s.configured()["postforme"] is True
        s = Settings.from_env({"POSTER_PROVIDER": "none"})
        assert isinstance(make_poster(s, http), ManualPoster)
        assert not any(k in s.configured() for k in ("postforme", "uploadpost", "postproxy"))
    with pytest.raises(ValueError, match="POSTER_PROVIDER"):
        Settings.from_env({"POSTER_PROVIDER": "buffer"})


@respx.mock
async def test_doctor_checks_only_the_selected_provider(capsys):
    route = respx.get(f"{PP}/api/profiles").respond(json={"data": [
        {"id": "prof2", "username": "clipz", "platform": "tiktok", "status": "active"}]})
    s = Settings.from_env({"POSTER_PROVIDER": "postproxy", "POSTPROXY_API_KEY": "k", "POSTFORME_API_KEY": "unused"})
    await doctor_run(s)
    out = capsys.readouterr().out
    assert route.call_count == 1 and "Poster: Postproxy" in out and "tiktok:clipz(prof2)" in out
    await doctor_run(Settings.from_env({"POSTER_PROVIDER": "none"}))
    assert "[SKIP] Poster: Manual" in capsys.readouterr().out


# ---- manual mode -------------------------------------------------
@respx.mock  # no routes: any HTTP call in manual mode would fail the test
async def test_manual_mode_approve_marks_approved_and_offers_download(settings, tmp_path):
    settings.poster_provider = "none"
    settings.default_accounts = ""
    http = httpx.AsyncClient()
    db = Database(":memory:")
    pl = Pipeline(settings, db, {}, DiscordNotifier("", http), make_poster(settings, http))
    video = tmp_path / "clip1.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 100)
    sid = db.start_session("twitch", "speed", "t", "1", str(tmp_path))
    cid = db.add_clip(sid, "twitch", "speed", 10, 80, 60, 5.5, 5.0, 3.0)
    db.update_clip(cid, status="pending", path=str(video), thumb="", caption="cap")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(pl, start_pipeline=False)),
                                 base_url="http://t") as c:
        st = (await c.get("/api/state")).json()
        assert st["poster"] == {"provider": "none", "label": "Manual (download)", "manual": True, "enabled": False,
                                "env_key": ""}
        assert (await c.get("/api/accounts")).json() == []
        r = await c.post(f"/api/clips/{cid}/approve", json={"caption": "mine"})  # no accounts, no API key
        assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["has_video"] is True
        await pl.wait_idle()
        clip = db.get_clip(cid)
        assert clip["status"] == "approved" and clip["caption"] == "mine" and not clip["post_id"]
        assert db.counts()["approved"] == 1 and db.count_posted_since(0) == 0
        dl = await c.get(f"/api/clips/{cid}/video", params={"download": "1"})
        assert dl.status_code == 200 and dl.content == video.read_bytes()
        assert 'attachment; filename="speed-clip-1.mp4"' in dl.headers["content-disposition"]
        again = await c.post(f"/api/clips/{cid}/approve", json={})
        assert again.status_code == 400 and "approved" in again.json()["detail"]
    with pytest.raises(ClipError, match="not found"):
        await pl.approve(999)
    await http.aclose()
