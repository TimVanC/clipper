import json

import httpx
import pytest
import respx

from app.notifier import DiscordNotifier
from app.poster import PostForMe, PostForMeError

pytestmark = pytest.mark.unit
BASE = "https://api.postforme.dev/v1"


@respx.mock
async def test_publish_uploads_then_posts_with_public_media_url(tmp_path):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"VIDEO")
    up = respx.post(f"{BASE}/media/create-upload-url").respond(
        json={"upload_url": "https://store.example/put?sig=1", "media_url": "https://cdn.example/m1.mp4"})
    put = respx.put("https://store.example/put?sig=1").respond(200)
    post = respx.post(f"{BASE}/social-posts").respond(json={"id": "sp_9", "status": "processing"})
    async with httpx.AsyncClient() as http:
        res = await PostForMe("KEY", http, BASE).publish(f, "hello #fyp", ["spc_1", "spc_2"], external_id="clip-3")
    assert res["post_id"] == "sp_9"
    assert up.calls[0].request.headers["Authorization"] == "Bearer KEY"
    assert put.calls[0].request.content == b"VIDEO"
    body = json.loads(post.calls[0].request.content)
    assert body == {"caption": "hello #fyp", "social_accounts": ["spc_1", "spc_2"],
                    "media": [{"url": "https://cdn.example/m1.mp4"}], "external_id": "clip-3"}


@respx.mock
async def test_errors_are_surfaced(tmp_path):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"x")
    respx.post(f"{BASE}/media/create-upload-url").respond(401, json={"message": "bad key"})
    async with httpx.AsyncClient() as http:
        p = PostForMe("KEY", http, BASE)
        with pytest.raises(PostForMeError, match="401"):
            await p.publish(f, "c", ["a"])
        with pytest.raises(PostForMeError, match="no social accounts"):
            await p.create_post("c", [], "https://m")


@respx.mock
async def test_list_accounts_shape():
    respx.get(f"{BASE}/social-accounts").respond(json={"data": [
        {"id": "spc_1", "platform": "tiktok", "username": "clips1", "status": "connected"}]})
    async with httpx.AsyncClient() as http:
        accts = await PostForMe("KEY", http, BASE).list_accounts()
    assert accts == [{"id": "spc_1", "platform": "tiktok", "username": "clips1", "status": "connected"}]


@respx.mock
async def test_discord_clip_message_attaches_small_files_only(tmp_path):
    hook = respx.post("https://discord.test/hook").respond(204)
    small, big = tmp_path / "s.mp4", tmp_path / "b.mp4"
    small.write_bytes(b"a" * 1000)
    big.write_bytes(b"a" * 3_000_000)
    clip = lambda p: {"id": 7, "handle": "speed", "start_s": 0, "end_s": 60, "score": 5.5, "audio_z": 6.0,
                      "chat_z": 2.0, "path": str(p)}
    async with httpx.AsyncClient() as http:
        n = DiscordNotifier("https://discord.test/hook", http, "https://clip.app", max_upload_mb=1)
        assert await n.notify_clip(clip(small))
        assert await n.notify_clip(clip(big))
        assert await n.notify_clip(clip(big), preview=small)  # too big -> falls back to the preview
    first, second, third = (hook.calls[i].request for i in range(3))
    assert first.headers["content-type"].startswith("multipart/form-data") and b"payload_json" in first.content
    assert second.headers["content-type"].startswith("application/json")
    assert third.headers["content-type"].startswith("multipart/form-data") and b"s.mp4" in third.content
    assert "https://clip.app/#clip-7" in second.content.decode()


async def test_discord_disabled_is_noop():
    async with httpx.AsyncClient() as http:
        n = DiscordNotifier("", http)
        assert not n.enabled and await n.notify_live("twitch", "x", "t", "u") is False


@respx.mock
async def test_discord_failure_does_not_raise():
    respx.post("https://discord.test/hook").respond(500)
    async with httpx.AsyncClient() as http:
        assert await DiscordNotifier("https://discord.test/hook", http).test() is False
