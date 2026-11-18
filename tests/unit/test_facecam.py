import numpy as np
import pytest

import app.facecam as fc
from app.clipper import build_filtergraph
from app.facecam import (CAM_ASPECT, GAME_ASPECT, cam_border, cam_candidates, cam_crop, choose_layout, face_motion,
                         game_crop, parse_boxes, pick_cam)

pytestmark = pytest.mark.unit
FACE = (0.07, 0.24, 0.056, 0.117)


def test_pick_cam_keeps_the_face_that_stays_put():
    rng = np.random.default_rng(1)
    dets = []
    for i in range(10):
        frame = [(float(rng.uniform(0.3, 0.8)), float(rng.uniform(0.2, 0.8)), 0.04, 0.08)]  # game faces wander
        if i % 3:  # the cam face is found in 7 of 10 frames, jittering a little
            frame.append((0.07 + i * 0.001, 0.24 - i * 0.001, 0.056, 0.117))
        dets.append(frame)
    box = pick_cam(dets)
    assert box is not None and box[0] == pytest.approx(0.074, abs=0.003) and box[3] == pytest.approx(0.117)
    assert pick_cam([[FACE]] * 3 + [[]] * 7) is None             # found in too few frames
    assert pick_cam([[(0.3, 0.1, 0.2, 0.5)]] * 10) is None        # full-screen camera, not an overlay
    assert pick_cam([]) is None and cam_candidates([[], []]) == []
    two = cam_candidates([[FACE, (0.8, 0.7, 0.03, 0.06)]] * 10)   # both steady: the bigger one first
    assert len(two) == 2 and two[0][2] == pytest.approx(0.056)


def test_cam_crop_has_the_panel_shape_and_contains_the_face():
    for fw, fh in ((1920, 1080), (1280, 720)):
        x, y, w, h = cam_crop(FACE, fw, fh)
        assert (w * fw) / (h * fh) == pytest.approx(CAM_ASPECT, rel=1e-6)
        assert 0 <= x and 0 <= y and x + w <= 1 + 1e-9 and y + h <= 1 + 1e-9
        assert x <= FACE[0] and FACE[0] + FACE[2] <= x + w and y <= FACE[1] and FACE[1] + FACE[3] <= y + h
    x, y, w, h = cam_crop((0.97, 0.95, 0.03, 0.05), 1920, 1080)   # face in a corner: crop stays in frame
    assert x + w <= 1 + 1e-9 and y + h <= 1 + 1e-9


def test_game_crop_is_centred_full_height_and_steps_around_the_cam():
    x, y, w, h = game_crop(None, 1920, 1080)
    assert (y, h) == (0, 1) and (w * 1920) / 1080 == pytest.approx(GAME_ASPECT) and x == pytest.approx((1 - w) / 2)
    cam = (0.0, 0.1, 0.30, 0.3)  # overlaps the centred slice -> moved right of it
    gx, _, gw, _ = game_crop(cam, 1920, 1080)
    assert gx == pytest.approx(0.30) and gx + gw <= 1
    small = (0.0, 0.1, 0.2, 0.25)  # doesn't overlap -> stays centred
    assert game_crop(small, 1920, 1080)[0] == pytest.approx(x)


def _frames_with_cam(n=10, moving=True):
    """A flat 'game' whose brightness changes every frame, and a cam box (x 100-400, y 50-250) with a face."""
    rng = np.random.default_rng(0)
    frames = []
    for i in range(n):
        f = np.full((540, 960, 3), int(rng.integers(0, 255)), np.uint8)
        f[50:250, 100:400] = 120
        f[100:170, 220:280] = 160 + (int(rng.integers(0, 60)) if moving else 0)
        frames.append(f)
    return frames


def test_cam_border_snaps_to_the_overlay_edges():
    face = (220 / 960, 100 / 540, 60 / 960, 70 / 540)
    box, sides = cam_border(_frames_with_cam(), face, cam_crop(face, 960, 540))
    assert sides == 4
    assert box == pytest.approx((100 / 960, 50 / 540, 300 / 960, 200 / 540), abs=1e-6)
    flat = [np.full((540, 960, 3), 90, np.uint8)] * 5  # no edges anywhere -> the face estimate stays
    fallback = cam_crop(face, 960, 540)
    box, sides = cam_border(flat, face, fallback)
    assert sides == 0 and box == pytest.approx(fallback)


def test_face_motion_tells_a_webcam_from_a_picture():
    face = (220 / 960, 100 / 540, 60 / 960, 70 / 540)
    assert face_motion(_frames_with_cam(moving=True), face) > 3
    # a picture of a face stays the same even while the game around it changes
    assert face_motion(_frames_with_cam(moving=False), face) < 1
    assert face_motion([np.full((540, 960, 3), 80, np.uint8)] * 6, face) == 0.0


def test_parse_boxes():
    assert parse_boxes("CaseOh_=0.7,0,0.3,0.3; @n3on=0,0.6,0.25,0.4\n") == {
        "caseoh_": (0.7, 0.0, 0.3, 0.3), "n3on": (0.0, 0.6, 0.25, 0.4)}
    assert parse_boxes("") == {}
    for bad in ("x=1,2,3", "x=0.9,0,0.3,0.3", "=0,0,1,1", "x=a,b,c,d"):
        with pytest.raises(ValueError):
            parse_boxes(bad)


def test_choose_layout(monkeypatch):
    monkeypatch.setattr(fc, "probe_size", lambda p: (1920, 1080))
    pieces = [("a.ts", 0.0, 60.0)]
    calls = []
    monkeypatch.setattr(fc, "find_facecam", lambda p, w, h, ff="ffmpeg": calls.append(1) or (0.0, 0.1, 0.2, 0.25))
    assert choose_layout(pieces, "blur", "Fortnite", "x") == ("blur", None)
    assert choose_layout(pieces, "auto", "Just Chatting", "x") == ("blur", None) and not calls
    lay, (cam, game) = choose_layout(pieces, "auto", "Grand Theft Auto VI (GTA)", "x")
    assert lay == "split" and cam == (0.0, 0.1, 0.2, 0.25) and game[3] == 1 and calls
    lay, (cam, _) = choose_layout(pieces, "auto", "Fortnite", "CaseOh_", "caseoh_=0.7,0,0.3,0.3")
    assert lay == "split" and cam == (0.7, 0.0, 0.3, 0.3) and len(calls) == 1  # pinned: no detection
    monkeypatch.setattr(fc, "find_facecam", lambda *a: None)
    assert choose_layout(pieces, "auto", "Fortnite", "x") == ("blur", None)
    assert choose_layout(pieces, "split", "", "x") == ("blur", None)  # split forced, but no cam found


def test_split_filtergraph():
    g = build_filtergraph(2, "split", None, ((0.0, 0.1, 0.2, 0.25), (0.25, 0.0, 0.5, 1.0)))
    assert "vstack=inputs=2" in g and "scale=1080:768" in g and "scale=1080:1152" in g
    assert "crop=w=trunc(iw*0.20000/2)*2:h=trunc(ih*0.25000/2)*2:x=trunc(iw*0.00000):y=trunc(ih*0.10000)" in g
    assert "vstack" not in build_filtergraph(1, "split", None, None)  # no cam -> blur
