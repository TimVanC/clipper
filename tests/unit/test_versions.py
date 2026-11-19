import subprocess

import httpx
import numpy as np
import pytest
import respx

from app import headline as hl
from app.clipper import CLASSIC_TOP, build_filtergraph, probe_duration, render_clip
from app.db import Database
from app.notifier import DiscordNotifier
from app.pipeline import ClipError, Pipeline
from app.posters import make_poster

UP = "https://api.upload-post.com"


def _frame(path, t=1.0):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "gray", "pipe:1"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(1920, 1080)


def test_full_frame_layout_puts_the_whole_frame_on_black(tmp_path):
    assert f"pad=1080:1920:0:{CLASSIC_TOP}:black" in build_filtergraph(1, "classic")
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=1280x720:r=30:d=3",
                    "-f", "lavfi", "-i", "sine=d=3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    out = tmp_path / "full.mp4"
    render_clip([(str(src), 0.5, 2.0)], out, "classic")
    img = _frame(out)
    assert img[CLASSIC_TOP + 300, 540] > 200 and img[CLASSIC_TOP - 40, 540] < 20 and img[1500, 540] < 20
    titled = tmp_path / "titled.mp4"
    hl.apply(out, titled, "CASEOH'S TIMING NEVER FAILS", CLASSIC_TOP, style="plain")
    top = _frame(titled)[:CLASSIC_TOP]
    assert (top > 200).sum() > 3000  # white text above the frame...
    assert top[CLASSIC_TOP - 20:].max() < 40  # ...ending just above it
    assert probe_duration(titled) == pytest.approx(2.0, abs=0.2)


def test_plain_style_has_no_box(tmp_path):
    from PIL import Image
    w, h = hl.render_png("NO WAY", tmp_path / "p.png", style="plain")
    img = np.array(Image.open(tmp_path / "p.png"))
    assert (img[..., 3] > 0).mean() < 0.3  # mostly transparent: just the letters


@pytest.fixture
def pipeline(settings, tmp_path):
    settings.poster_provider, settings.uploadpost_api_key = "uploadpost", "uk"
    settings.default_accounts = "default:tiktok"
    http = httpx.AsyncClient()
    db = Database(":memory:")
    pl = Pipeline(settings, db, {}, DiscordNotifier("", http), make_poster(settings, http))
    yield pl
    return


def _clip(pl, tmp_path, versions=("headline", "plain", "full")):
    cid = pl.db.add_clip(pl.db.start_session("twitch", "x", "t", "1", str(tmp_path)), "twitch", "x", 0, 61, 40, 6, 5, 4)
    main = pl.clips_dir / f"{cid}.mp4"
    main.write_bytes(b"MAIN")
    if "plain" in versions:
        pl.base_path(cid).write_bytes(b"PLAIN")
    if "full" in versions:
        pl.full_path(cid).write_bytes(b"FULL")
    pl.db.update_clip(cid, status="pending", path=str(main), caption="cap", headline="hi")
    return cid


@respx.mock
@pytest.mark.parametrize("version,expected", [("headline", b"MAIN"), ("plain", b"PLAIN"), ("full", b"FULL")])
async def test_approve_posts_the_chosen_version(pipeline, tmp_path, version, expected):
    route = respx.post(f"{UP}/api/upload").respond(json={"success": True, "request_id": "r"})
    cid = _clip(pipeline, tmp_path)
    await pipeline.approve(cid, version=version)
    await pipeline.wait_idle()
    assert pipeline.db.get_clip(cid)["status"] == "posted"
    assert expected in route.calls[0].request.content


async def test_full_frame_version_has_no_text(pipeline, tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=1280x720:r=30:d=3",
                    "-f", "lavfi", "-i", "sine=d=3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    cid = _clip(pipeline, tmp_path, versions=())
    await pipeline.make_full_version(cid, [(str(src), 0.5, 2.0)])
    assert pipeline.full_file(cid) == pipeline.full_path(cid) and not pipeline.full_path(cid, base=True).exists()
    assert _frame(pipeline.full_path(cid))[:CLASSIC_TOP].max() < 40  # nothing above the frame
    # older clips kept a titled <id>_full.mp4 next to a text-free <id>_full_base.mp4: post the text-free one
    old = _clip(pipeline, tmp_path)
    pipeline.full_path(old, base=True).write_bytes(b"NOTEXT")
    clip = pipeline.db.get_clip(old)
    assert pipeline.version_file(clip, "full").read_bytes() == b"NOTEXT"
    assert dict(pipeline.posting_files(clip, "all"))["full"].read_bytes() == b"NOTEXT"


async def test_versions_fall_back_and_reject_unknown(pipeline, tmp_path):
    cid = _clip(pipeline, tmp_path, versions=())
    clip = pipeline.db.get_clip(cid)
    assert pipeline.version_file(clip, "full").name == f"{cid}.mp4"  # not made: the main clip
    with pytest.raises(ClipError):
        await pipeline.approve(cid, version="sideways")
    pipeline.reject(cid)
    assert not pipeline.base_path(cid).exists() and not pipeline.full_path(cid).exists()


@respx.mock
async def test_all_versions_tiktok_gets_one_youtube_gets_all(pipeline, tmp_path):
    route = respx.post(f"{UP}/api/upload").respond(json={"success": True, "request_id": "r"})
    pipeline.s.default_accounts = "default:tiktok,default:youtube"
    pipeline.s.max_posts_per_day = 4
    cid = _clip(pipeline, tmp_path)
    await pipeline.approve(cid, version="all")
    await pipeline.wait_idle()
    clip = pipeline.db.get_clip(cid)
    bodies = [c.request.content for c in route.calls]
    assert clip["status"] == "posted" and not clip["post_error"]
    assert clip["post_id"] == "headline=r,plain=r@default:youtube,full=r@default:youtube"
    assert [b"MAIN" in b for b in bodies] == [True, False, False] and b"FULL" in bodies[2]
    assert [b"tiktok" in b for b in bodies] == [True, False, False] and all(b"youtube" in b for b in bodies)
    assert all(b'name="external_id"' in b for b in bodies) and b"clip-%d-full" % cid in bodies[2]
    assert pipeline.uploads_today() == {"default:tiktok": 1, "default:youtube": 3}
    # YouTube has 1 of its 4 left: the next clip goes out once there, TikTok still gets its version
    second = _clip(pipeline, tmp_path)
    await pipeline.approve(second, version="all")
    await pipeline.wait_idle()
    clip = pipeline.db.get_clip(second)
    assert clip["status"] == "posted" and clip["post_id"] == "headline=r" and len(route.calls) == 4
    pipeline.s.tiktok_version = "full"  # TikTok's pick; YouTube is full: only TikTok, with the full frame
    third = _clip(pipeline, tmp_path)
    await pipeline.approve(third, version="all")
    await pipeline.wait_idle()
    clip = pipeline.db.get_clip(third)
    assert clip["post_id"] == "full=r@default:tiktok" and "youtube: daily cap" in clip["post_error"]
    assert b"FULL" in route.calls[4].request.content
    pipeline.s.max_posts_per_day = 2  # both at their cap now: refused outright
    with pytest.raises(ClipError, match="daily post cap reached for youtube, tiktok"):
        await pipeline.approve(_clip(pipeline, tmp_path), version="all")


def test_old_post_ids_count_every_account(pipeline, tmp_path):
    a, b = _clip(pipeline, tmp_path), _clip(pipeline, tmp_path)
    pipeline.db.update_clip(a, status="posted", accounts="default:tiktok,default:youtube",
                            post_id="headline=x,plain=y,full=z")  # before: every version went everywhere
    pipeline.db.update_clip(b, status="posted", accounts="default:tiktok,default:youtube", post_id="sp")
    for cid in (a, b):
        pipeline.db.claim_clip(cid, ("posted",), "posted")
    assert pipeline.uploads_today() == {"default:tiktok": 4, "default:youtube": 4}


@respx.mock
async def test_cap_ignores_posts_made_through_a_previous_provider(pipeline, tmp_path):
    respx.post(f"{UP}/api/upload").respond(json={"success": True, "request_id": "r"})
    pipeline.s.max_posts_per_day, pipeline.s.tiktok_version = 3, "all"
    old = _clip(pipeline, tmp_path)  # posted earlier today through Postproxy + Upload-Post combined
    pipeline.db.update_clip(old, status="posted", accounts="postproxy/AnUZrZ,uploadpost/default:youtube",
                            post_id="postproxy:x,uploadpost:y")
    pipeline.db.claim_clip(old, ("posted",), "posted")  # sets decided_at = now
    assert not pipeline.same_setup("postproxy/AnUZrZ,uploadpost/default:youtube")
    assert pipeline.same_setup("default:tiktok,default:youtube")
    cid = _clip(pipeline, tmp_path)
    await pipeline.approve(cid, version="all")  # 0 counted + 3 <= 3
    await pipeline.wait_idle()
    assert pipeline.db.get_clip(cid)["status"] == "posted"


@respx.mock
async def test_all_versions_partial_failure(pipeline, tmp_path):
    respx.post(f"{UP}/api/upload").mock(side_effect=[
        httpx.Response(200, json={"success": True, "request_id": "a"}), httpx.Response(500, text="boom"),
        httpx.Response(200, json={"success": True, "request_id": "c"})])
    pipeline.s.tiktok_version = "all"
    cid = _clip(pipeline, tmp_path)
    await pipeline.approve(cid, version="all")
    await pipeline.wait_idle()
    clip = pipeline.db.get_clip(cid)
    assert clip["status"] == "posted" and clip["post_id"] == "headline=a,full=c" and "plain" in clip["post_error"]


async def test_clip_without_headline_has_no_separate_plain_version(pipeline, tmp_path):
    cid = _clip(pipeline, tmp_path)
    pipeline.db.update_clip(cid, headline="")
    assert [v for v, _ in pipeline.posting_files(pipeline.db.get_clip(cid), "all")] == ["headline", "full"]


def test_auto_post_by_rating(pipeline):
    s = pipeline.s
    s.auto_approve, s.auto_post_min_rating = False, 0
    assert not pipeline.should_auto_post(9.9)          # off by default
    s.auto_post_min_rating = 7.5
    assert pipeline.should_auto_post(7.5) and pipeline.should_auto_post(9.1) and not pipeline.should_auto_post(7.4)
    s.auto_post_min_rating, s.auto_approve = 0, True
    assert pipeline.should_auto_post(1.0)               # auto_approve still posts everything
    s.update({"auto_post_version": "full"})
    with pytest.raises(ValueError):
        s.update({"auto_post_version": "best"})
