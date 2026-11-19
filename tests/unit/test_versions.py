import subprocess
import time

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


def _uploads(route_ids=None):
    """Mock Upload-Post's upload endpoint: request ids r1, r2, ..."""
    n = iter(range(1, 1000))
    return respx.post(f"{UP}/api/upload").mock(
        side_effect=lambda req: httpx.Response(200, json={"success": True, "request_id": f"r{next(n)}"}))


def _status(results: dict):
    """Mock the status endpoint: request id -> one result dict (missing = still processing)."""
    def answer(req):
        res = results.get(req.url.params["request_id"])
        if res is None:
            return httpx.Response(200, json={"status": "pending", "results": []})
        return httpx.Response(200, json={"status": "completed", "results": [res]})
    return respx.get(f"{UP}/api/uploadposts/status").mock(side_effect=answer)


def _live(url="https://www.tiktok.com/@x/video/1"):
    return {"platform": "tiktok", "success": True, "post_url": url}


YT_LIMIT = {"platform": "youtube", "success": False, "error_code": "youtube_channel_daily_limit",
            "error_message": "This YouTube channel has reached the number of videos it may upload today. "
                             "The limit is set by YouTube per channel."}
TT_SPAM = {"platform": "tiktok", "success": False, "error_code": "unknown_error",
           "error_message": "This TikTok creator has made too many posts via OpenAPI in the last 24 hours, so "
                            "this publishing attempt is blocked to minimize spam. Try later."}


def _uploads_of(pl, cid):
    return [(u["version"], u["account"].split(":")[-1], u["state"]) for u in pl.db.uploads_for(cid)]


@respx.mock
@pytest.mark.parametrize("version,expected", [("headline", b"MAIN"), ("plain", b"PLAIN"), ("full", b"FULL")])
async def test_approve_posts_the_chosen_version_and_checks_it_went_live(pipeline, tmp_path, version, expected):
    route = _uploads()
    status = _status({})
    cid = _clip(pipeline, tmp_path)
    assert (await pipeline.approve(cid, version=version))["status"] == "queued"
    await pipeline.wait_idle()
    assert expected in route.calls[0].request.content and b"clip-%d-%s" % (cid, version.encode()) in route.calls[0].request.content
    assert _uploads_of(pipeline, cid) == [(version, "tiktok", "processing")]
    await pipeline.check_uploads()  # the network hasn't answered yet: still queued
    assert pipeline.db.get_clip(cid)["status"] == "queued" and status.call_count == 1
    status.side_effect = _status({"r1": _live("https://tt/1")}).side_effect
    await pipeline.check_uploads()
    clip = pipeline.db.get_clip(cid)
    assert clip["status"] == "posted" and clip["post_id"] == "r1" and not clip["post_error"]
    assert pipeline.db.uploads_for(cid)[0]["url"] == "https://tt/1"
    assert clip["path"] == "" and not (pipeline.clips_dir / f"{cid}.mp4").exists()  # it's live: no local copy
    assert not pipeline.base_path(cid).exists() and not pipeline.full_path(cid).exists()


async def test_full_frame_version_has_no_text(pipeline, tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=1280x720:r=30:d=3",
                    "-f", "lavfi", "-i", "sine=d=3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    cid = _clip(pipeline, tmp_path, versions=())
    pipeline.s.auto_post_version = "full"  # only rendered when a network posts it
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


def test_each_network_gets_its_own_version(pipeline, tmp_path):
    clip = pipeline.db.get_clip(_clip(pipeline, tmp_path))
    both = ["default:tiktok", "default:youtube"]
    assert pipeline.jobs_for(clip, "all", both) == [("headline", "default:tiktok"), ("headline", "default:youtube")]
    pipeline.s.youtube_version, pipeline.s.tiktok_version = "all", "full"
    assert pipeline.jobs_for(clip, "all", both) == [("full", "default:tiktok"), ("headline", "default:youtube"),
                                                     ("plain", "default:youtube"), ("full", "default:youtube")]
    assert pipeline.jobs_for(clip, "plain", both) == [("plain", "default:tiktok"), ("plain", "default:youtube")]


@respx.mock
async def test_queue_respects_daily_allowance_gap_and_rating(pipeline, tmp_path):
    route = _uploads()
    pl, s = pipeline, pipeline.s
    s.tiktok_per_day, s.post_gap_min = 3, 60
    earlier = pl.db.add_upload(_clip(pl, tmp_path), "headline", "default:tiktok", state="live")  # just posted
    low, high, mid = (_clip(pl, tmp_path) for _ in range(3))
    for cid, score in ((low, 2.0), (high, 9.0), (mid, 5.0)):
        pl.db._exec("UPDATE clips SET score=? WHERE id=?", (score, cid))
        await pl.approve(cid)
    await pl.wait_idle()
    assert route.call_count == 0 and {pl.db.get_clip(c)["status"] for c in (low, high, mid)} == {"queued"}
    assert pl.slot_at("default:tiktok") == pytest.approx(time.time() + 3600, abs=5)  # the gap
    pl.db._exec("UPDATE uploads SET sent_at=? WHERE id=?", (time.time() - 7200, earlier))
    await pl.run_queue()  # one slot: the best-rated clip goes first, the gap holds the others
    assert route.call_count == 1 and _uploads_of(pl, high) == [("headline", "tiktok", "processing")]
    s.post_gap_min = 0
    await pl.run_queue()  # 3 a day: the earlier post + 2 from the queue
    assert route.call_count == 2 and pl.db.uploads_for(mid) and not pl.db.uploads_for(low)
    assert pl.db.get_clip(low)["status"] == "queued"
    assert pl.slot_at("default:tiktok") == pytest.approx(time.time() - 7200 + 86400, abs=5)
    slots = pl.posting_slots()
    assert [(x["network"], x["used"], x["cap"]) for x in slots] == [("tiktok", 3, 3)]


@respx.mock
async def test_turned_down_uploads_are_retried_later_and_reported(pipeline, tmp_path):
    _uploads()
    pl = pipeline
    pl.s.default_accounts, pl.s.post_gap_min = "default:tiktok,default:youtube", 0
    cid = _clip(pl, tmp_path)
    await pl.approve(cid, version="all")
    await pl.wait_idle()
    assert sorted(_uploads_of(pl, cid)) == [("headline", "tiktok", "processing"), ("headline", "youtube", "processing")]
    by_net = {u["account"]: u["request_id"] for u in pl.db.uploads_for(cid)}
    _status({by_net["default:tiktok"]: _live(), by_net["default:youtube"]: YT_LIMIT})
    await pl.check_uploads()
    # YouTube said no for today: still queued for it, and YouTube rests before the next try
    assert pl.db.get_clip(cid)["status"] == "queued"
    assert pl.slot_at("default:youtube") > time.time() + 2 * 3600
    assert ("headline", "youtube", "limited") in _uploads_of(pl, cid)
    pl.db._exec("UPDATE clips SET decided_at=? WHERE id=?", (time.time() - 13 * 3600, cid))  # waited too long
    await pl.run_queue()
    clip = pl.db.get_clip(cid)
    assert clip["status"] == "posted" and "youtube headline: not posted (This YouTube channel has reached" in clip["post_error"]
    # nothing went live and only limits: back to Review; a retry clears the old attempts
    other = _clip(pl, tmp_path)
    pl.s.default_accounts = "default:tiktok"
    pl.db._exec("UPDATE uploads SET sent_at=sent_at-86400")  # yesterday: frees TikTok's slots
    await pl.approve(other)
    await pl.wait_idle()
    rid = pl.db.uploads_for(other)[0]["request_id"]
    _status({rid: TT_SPAM})
    await pl.check_uploads()
    pl.db._exec("UPDATE clips SET decided_at=? WHERE id=?", (time.time() - 13 * 3600, other))
    await pl.run_queue()
    back = pl.db.get_clip(other)
    assert back["status"] == "pending" and "too many posts" in back["post_error"] and "Approve it again" in back["post_error"]
    pl.db._exec("UPDATE uploads SET sent_at=sent_at-86400")
    await pl.approve(other)
    await pl.wait_idle()
    assert [u["state"] for u in pl.db.uploads_for(other)] == ["processing"]


@respx.mock
async def test_upload_errors_fail_the_clip_and_restart_cuts_are_failed(pipeline, tmp_path):
    respx.post(f"{UP}/api/upload").respond(500, text="boom")
    cid = _clip(pipeline, tmp_path)
    await pipeline.approve(cid)
    await pipeline.wait_idle()
    clip = pipeline.db.get_clip(cid)
    assert clip["status"] == "failed" and "tiktok headline:" in clip["post_error"] and "500" in clip["post_error"]
    held = _clip(pipeline, tmp_path)
    pipeline.db.add_upload(held, "headline", "default:tiktok")  # 'sending' when the app stopped
    assert pipeline.db.fail_interrupted_uploads() == [held]
    assert pipeline.db.uploads_in("sending") == [] and "check the profile" in pipeline.db.uploads_for(held)[0]["error"]


@respx.mock
async def test_all_versions_partial_failure(pipeline, tmp_path):
    respx.post(f"{UP}/api/upload").mock(side_effect=[
        httpx.Response(200, json={"success": True, "request_id": "a"}), httpx.Response(500, text="boom"),
        httpx.Response(200, json={"success": True, "request_id": "c"})])
    _status({"a": _live(), "c": _live()})
    pipeline.s.tiktok_version, pipeline.s.post_gap_min = "all", 0
    cid = _clip(pipeline, tmp_path)
    await pipeline.approve(cid, version="all")
    await pipeline.wait_idle()
    await pipeline.check_uploads()
    clip = pipeline.db.get_clip(cid)
    assert clip["status"] == "posted" and clip["post_id"] == "a,c" and "tiktok plain:" in clip["post_error"]


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
