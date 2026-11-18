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


async def test_versions_fall_back_and_reject_unknown(pipeline, tmp_path):
    cid = _clip(pipeline, tmp_path, versions=())
    clip = pipeline.db.get_clip(cid)
    assert pipeline.version_file(clip, "full").name == f"{cid}.mp4"  # not made: the main clip
    with pytest.raises(ClipError):
        await pipeline.approve(cid, version="sideways")
    pipeline.reject(cid)
    assert not pipeline.base_path(cid).exists() and not pipeline.full_path(cid).exists()


@respx.mock
async def test_all_versions_are_posted_separately_and_counted_against_the_cap(pipeline, tmp_path):
    route = respx.post(f"{UP}/api/upload").respond(json={"success": True, "request_id": "r"})
    pipeline.s.max_posts_per_day = 4
    cid = _clip(pipeline, tmp_path)
    await pipeline.approve(cid, version="all")
    await pipeline.wait_idle()
    clip = pipeline.db.get_clip(cid)
    bodies = [c.request.content for c in route.calls]
    assert clip["status"] == "posted" and clip["post_id"] == "headline=r,plain=r,full=r"
    assert [b"MAIN" in b for b in bodies] == [True, False, False] and b"FULL" in bodies[2]
    assert all(b'name="external_id"' in b for b in bodies) and b"clip-%d-full" % cid in bodies[2]
    assert pipeline.db.count_uploads_since(0) == 3
    second = _clip(pipeline, tmp_path)  # 3 + 3 > 4: refused; one more single upload still fits
    with pytest.raises(ClipError, match="daily post cap"):
        await pipeline.approve(second, version="all")
    await pipeline.approve(second, version="headline")
    await pipeline.wait_idle()


@respx.mock
async def test_all_versions_partial_failure(pipeline, tmp_path):
    respx.post(f"{UP}/api/upload").mock(side_effect=[
        httpx.Response(200, json={"success": True, "request_id": "a"}), httpx.Response(500, text="boom"),
        httpx.Response(200, json={"success": True, "request_id": "c"})])
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
