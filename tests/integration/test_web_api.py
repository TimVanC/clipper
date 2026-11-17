import json

import httpx
import pytest
import respx

from app.db import Database
from app.notifier import DiscordNotifier
from app.pipeline import Pipeline
from app.poster import PostForMe
from app.web import create_app

pytestmark = pytest.mark.integration
PFM = "https://api.postforme.dev/v1"


@pytest.fixture
async def env(settings, tmp_path):
    settings.postforme_api_key = "KEY"
    http = httpx.AsyncClient()
    db = Database(":memory:")
    pl = Pipeline(settings, db, {}, DiscordNotifier("", http), PostForMe("KEY", http, PFM))
    app = create_app(pl, start_pipeline=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    video = tmp_path / "clip1.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 100)
    sid = db.start_session("twitch", "speed", "t", "1", str(tmp_path))
    cid = db.add_clip(sid, "twitch", "speed", 10, 80, 60, 5.5, 5.0, 3.0)
    db.update_clip(cid, status="pending", path=str(video), thumb="", caption="cap")
    yield pl, client, cid
    await client.aclose()
    await http.aclose()


def mock_pfm():
    respx.post(f"{PFM}/media/create-upload-url").respond(
        json={"upload_url": "https://store.example/up", "media_url": "https://cdn.example/v.mp4"})
    respx.put("https://store.example/up").respond(200)
    return respx.post(f"{PFM}/social-posts").respond(json={"id": "sp_1"})


async def test_index_state_and_streamer_crud(env):
    pl, c, _ = env
    r = await c.get("/")
    assert r.status_code == 200 and "Clipper" in r.text and "/api/state" in r.text
    assert (await c.get("/healthz")).json() == {"ok": True}
    sid = (await c.post("/api/streamers", json={"platform": "kick", "handle": "AdinRoss"})).json()["id"]
    assert (await c.post("/api/streamers", json={"platform": "tiktok", "handle": "x"})).status_code == 400
    st = (await c.get("/api/state")).json()
    assert st["streamers"][0]["handle"] == "adinross" and st["counts"]["pending"] == 1
    assert st["settings"]["pre_roll_s"] == 40.0 and st["configured"]["postforme"] is True
    await c.patch(f"/api/streamers/{sid}", params={"enabled": "false"})
    assert pl.db.list_streamers(enabled_only=True) == []
    await c.delete(f"/api/streamers/{sid}")
    assert pl.db.list_streamers() == []


async def test_settings_validation_and_persistence(env):
    pl, c, _ = env
    r = await c.put("/api/settings", json={"z_threshold": "2.5", "layout": "crop", "auto_approve": True})
    assert r.status_code == 200 and pl.s.z_threshold == 2.5 and pl.s.layout == "crop"
    assert pl.db.get_settings()["z_threshold"] == "2.5"
    bad = await c.put("/api/settings", json={"z_threshold": "-1"})
    assert bad.status_code == 400 and pl.s.z_threshold == 2.5
    pl.s.z_threshold = 3.0
    pl.load_overrides()  # what happens on restart
    assert pl.s.z_threshold == 2.5


@respx.mock
async def test_approve_posts_through_post_for_me(env):
    pl, c, cid = env
    post = mock_pfm()
    r = await c.post(f"/api/clips/{cid}/approve", json={"caption": "my caption", "accounts": ["spc_9"]})
    assert r.status_code == 200 and r.json()["status"] == "posting"
    await pl.wait_idle()
    clip = pl.db.get_clip(cid)
    assert clip["status"] == "posted" and clip["post_id"] == "sp_1" and clip["caption"] == "my caption"
    body = json.loads(post.calls[0].request.content)
    assert body["social_accounts"] == ["spc_9"] and body["media"] == [{"url": "https://cdn.example/v.mp4"}]
    # second approve is refused (no double posting)
    again = await c.post(f"/api/clips/{cid}/approve", json={})
    assert again.status_code == 400 and "posted" in again.json()["detail"]
    assert post.call_count == 1


@respx.mock
async def test_default_accounts_used_and_failure_is_retryable(env):
    pl, c, cid = env
    respx.post(f"{PFM}/media/create-upload-url").respond(500, text="boom")
    await c.post(f"/api/clips/{cid}/approve", json={})
    await pl.wait_idle()
    clip = pl.db.get_clip(cid)
    assert clip["status"] == "failed" and "500" in clip["post_error"] and clip["accounts"] == "spc_1"
    respx.clear()
    post = mock_pfm()
    assert (await c.post(f"/api/clips/{cid}/approve", json={})).status_code == 200  # retry works
    await pl.wait_idle()
    assert pl.db.get_clip(cid)["status"] == "posted" and post.call_count == 1


async def test_approve_guards(env):
    pl, c, cid = env
    pl.s.default_accounts = ""
    r = await c.post(f"/api/clips/{cid}/approve", json={})
    assert r.status_code == 400 and "account" in r.json()["detail"]
    assert (await c.post("/api/clips/999/approve", json={"accounts": ["a"]})).status_code == 400
    pl.s.max_posts_per_day = 1
    assert pl.db.claim_clip(cid, ("pending",), "posting")  # one already used today
    other = pl.db.add_clip(1, "twitch", "x", 0, 30, 20, 4, 4, 0)
    pl.db.update_clip(other, status="pending")           # but this one has no video yet
    r = await c.post(f"/api/clips/{other}/approve", json={"accounts": ["a"]})
    assert r.status_code == 400 and "no video" in r.json()["detail"]
    pl.db.update_clip(other, path=pl.db.get_clip(cid)["path"])
    r = await c.post(f"/api/clips/{other}/approve", json={"accounts": ["a"]})
    assert r.status_code == 400 and "cap" in r.json()["detail"]
    pl.poster.key = ""
    assert "not configured" in (await c.post(f"/api/clips/{other}/approve", json={"accounts": ["a"]})).json()["detail"]


async def test_reject_deletes_media_and_video_served(env):
    pl, c, cid = env
    v = await c.get(f"/api/clips/{cid}/video")
    assert v.status_code == 200 and v.headers["content-type"] == "video/mp4" and v.content.startswith(b"\x00\x00")
    assert (await c.get(f"/api/clips/{cid}/thumb")).status_code == 404
    listed = (await c.get("/api/clips", params={"status": "pending"})).json()
    assert listed[0]["id"] == cid and "path" not in listed[0] and listed[0]["has_video"] is True
    r = await c.post(f"/api/clips/{cid}/reject")
    assert r.json()["status"] == "rejected"
    assert (await c.get(f"/api/clips/{cid}/video")).status_code == 404
    assert (await c.post(f"/api/clips/{cid}/reject")).status_code == 400


async def test_basic_auth_protects_everything_but_healthz(env):
    pl, c, cid = env
    pl.s.gui_password = "s3cret"
    assert (await c.get("/healthz")).status_code == 200
    for path in ("/", "/api/state", f"/api/clips/{cid}/video"):
        assert (await c.get(path)).status_code == 401
    assert (await c.get("/api/state", auth=("admin", "wrong"))).status_code == 401
    assert (await c.get("/api/state", auth=("admin", "s3cret"))).status_code == 200


@respx.mock
async def test_accounts_endpoint(env):
    _, c, _ = env
    respx.get(f"{PFM}/social-accounts").respond(json={"data": [
        {"id": "spc_1", "platform": "tiktok", "username": "me", "status": "connected"}]})
    assert (await c.get("/api/accounts")).json()[0]["id"] == "spc_1"
    respx.clear()
    respx.get(f"{PFM}/social-accounts").respond(401, text="no")
    assert (await c.get("/api/accounts")).status_code == 502
