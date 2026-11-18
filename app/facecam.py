"""Split layout for game streams: the streamer's facecam on top, the gameplay below (the usual clip format).

find_facecam() samples frames from the clip window and runs OpenCV's YuNet face detector on them. The face
that sits in the same place in most frames is the facecam; faces in the game move around and are dropped.
A box around that face becomes the top panel, and a full-height slice of the game next to it the bottom one.

Face detection finds the face, not the cam's border, so the top panel is a zoom on the face region. When it
guesses wrong for a streamer, Settings -> facecam_boxes pins the cam position by hand.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np

from .captions import is_game
from .clipper import Piece, probe_size

log = logging.getLogger("clipper.facecam")

MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
Box = tuple[float, float, float, float]  # x, y, w, h as fractions of the frame

OUT_W = 1080
CAM_H, GAME_H = 768, 1152          # top / bottom panel heights in the 1080x1920 output (40 / 60)
CAM_ASPECT = OUT_W / CAM_H          # width / height of the cam crop, in pixels
GAME_ASPECT = OUT_W / GAME_H
SAMPLE_W, SAMPLE_H = 960, 540       # frames are analysed at this size
SAMPLES = 10
MIN_SHARE = 0.4                     # the cam face must be found in at least this share of the frames
MAX_FACE_H = 0.28                   # a bigger face means a full-screen camera, not a cam overlay
FACE_TO_CAM_W = 3.4                 # cam crop width, in face widths (head and shoulders)
MIN_SCORE = 0.75
MIN_MOTION = 3.0                    # pixel change around the face; pictures and menus measure ~1
MIN_SIDES = 2                       # straight cam-box edges found around the face


# ---- pure geometry (unit tested) ------------------------------------------------------------
def pick_cam(dets: list[list[Box]], min_share: float = MIN_SHARE, max_face_h: float = MAX_FACE_H) -> Box | None:
    """dets[i] = face boxes found in frame i. Returns the median box of the face that stays put, or None."""
    found = cam_candidates(dets, min_share, max_face_h)
    return found[0] if found else None


def cam_candidates(dets: list[list[Box]], min_share: float = MIN_SHARE, max_face_h: float = MAX_FACE_H) -> list[Box]:
    """Faces that stay in one place across the frames, best first (found in most frames, then biggest)."""
    n = len(dets)
    if not n:
        return []
    clusters: list[dict] = []  # {"cx", "cy", "boxes": [(frame, box)]}
    for i, boxes in enumerate(dets):
        for b in boxes:
            cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
            for c in clusters:
                if abs(c["cx"] - cx) < 0.05 and abs(c["cy"] - cy) < 0.08:
                    c["boxes"].append((i, b))
                    k = len(c["boxes"])
                    c["cx"] += (cx - c["cx"]) / k
                    c["cy"] += (cy - c["cy"]) / k
                    break
            else:
                clusters.append({"cx": cx, "cy": cy, "boxes": [(i, b)]})
    out = []
    for c in sorted(clusters, key=lambda c: (-len({f for f, _ in c["boxes"]}),
                                             -float(np.mean([b[2] * b[3] for _, b in c["boxes"]])))):
        if len({f for f, _ in c["boxes"]}) < max(2, int(np.ceil(min_share * n))):
            break
        box = tuple(float(v) for v in np.median(np.array([b for _, b in c["boxes"]]), axis=0))
        if box[3] <= max_face_h:
            out.append(box)
    return out  # type: ignore[return-value]


def face_motion(frames: list[np.ndarray], face: Box) -> float:
    """Average pixel change around a face across the frames: a webcam keeps changing, a picture does not."""
    H, W = frames[0].shape[:2]
    x, y, w, h = face
    x0, y0 = int(max(0, (x - w * 0.25) * W)), int(max(0, (y - h * 0.25) * H))
    x1, y1 = int(min(W, (x + w * 1.25) * W)), int(min(H, (y + h * 1.25) * H))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return 0.0
    g = np.stack([f[y0:y1, x0:x1].mean(axis=2) for f in frames]).astype(np.float32)
    return float(np.abs(g - np.median(g, axis=0)).mean())


def _fit(w: float, h: float, aspect: float, fw: float, fh: float) -> tuple[float, float]:
    if w > fw:
        w, h = fw, fw / aspect
    if h > fh:
        w, h = fh * aspect, fh
    return w, h


def cam_crop(face: Box, fw: int, fh: int) -> Box:
    """Crop box (fractions) around a face, with the top panel's aspect ratio: head and shoulders."""
    x, y, w, h = face[0] * fw, face[1] * fh, face[2] * fw, face[3] * fh
    cw = max(w * FACE_TO_CAM_W, h * 2.2 * CAM_ASPECT)
    cw, ch = _fit(cw, cw / CAM_ASPECT, CAM_ASPECT, fw, fh)
    cx, cy = x + w / 2, y + h / 2 + 0.2 * h  # a little below the face, so the shoulders show
    left = min(max(cx - cw / 2, 0.0), fw - cw)
    top = min(max(cy - ch / 2, 0.0), fh - ch)
    return (left / fw, top / fh, cw / fw, ch / fh)


def _edge(strength: np.ndarray, cover: np.ndarray, lo: float, hi: float, near_hi: bool) -> int | None:
    """The border line in [lo, hi) nearest the face: strong, standing out, and running across nearly the whole
    cam (text and objects in the room are broken lines). Returns the index just after the edge."""
    lo_i, hi_i = max(0, int(lo)), min(len(strength), int(hi))
    if hi_i - lo_i < 3:
        return None
    seg = strength[lo_i:hi_i]
    need = max(8.0, 3.0 * float(np.median(seg)))
    hits = [i for i in range(len(seg)) if seg[i] >= need and cover[lo_i + i] >= 0.85]
    if not hits:
        return None
    return lo_i + (max(hits) if near_hi else min(hits)) + 1


def cam_border(frames: list[np.ndarray], face: Box, fallback: Box) -> tuple[Box, int]:
    """Snap the cam crop to the overlay's real border. A cam overlay has straight edges that stay put while
    the game moves, so averaged over the sampled frames they stand out. A side with no clear edge (the cam
    touches the frame edge, or has no hard border) keeps the face-based estimate.
    Returns (box, number of sides that snapped to an edge)."""
    g = np.stack([f.mean(axis=2) for f in frames]).astype(np.float32)
    n, H, W = g.shape
    dy = np.abs(np.diff(g, axis=1)).mean(axis=0)  # (H-1, W): change between row y and y+1
    dx = np.abs(np.diff(g, axis=2)).mean(axis=0)  # (H, W-1)
    fx, fy, fw, fh = face[0] * W, face[1] * H, face[2] * W, face[3] * H
    xs = slice(max(0, int(fx - fw * 0.5)), min(W, int(fx + fw * 1.5)))
    ys = slice(max(0, int(fy)), min(H, int(fy + fh * 1.5)))
    rows, cols = dy[:, xs].mean(axis=1), dx[ys, :].mean(axis=0)
    rows_cover, cols_cover = (dy[:, xs] > 10).mean(axis=1), (dx[ys, :] > 10).mean(axis=0)
    fb = (fallback[0] * W, fallback[1] * H, (fallback[0] + fallback[2]) * W, (fallback[1] + fallback[3]) * H)
    left = _edge(cols, cols_cover, fx - fw * 3, fx - fw * 0.2, near_hi=True)
    right = _edge(cols, cols_cover, fx + fw * 1.2, fx + fw * 4, near_hi=False)
    top = _edge(rows, rows_cover, fy - fh * 3, fy - fh * 0.2, near_hi=True)
    bottom = _edge(rows, rows_cover, fy + fh * 1.3, fy + fh * 5, near_hi=False)
    x0 = left if left is not None else fb[0]
    x1 = right if right is not None else fb[2]
    y0 = top if top is not None else fb[1]
    y1 = bottom if bottom is not None else fb[3]
    if x1 - x0 < fw * 1.5 or y1 - y0 < fh * 1.5 or not (x0 <= fx and fx + fw <= x1 and y0 <= fy and fy + fh <= y1):
        return fallback, 0
    sides = sum(s is not None for s in (left, right, top, bottom))
    return (x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H), sides


def game_crop(cam: Box | None, fw: int, fh: int) -> Box:
    """Full-height slice of the frame for the bottom panel: centred, moved sideways to keep the cam out of it."""
    gw, gh = _fit(fh * GAME_ASPECT, fh, GAME_ASPECT, fw, fh)
    x = (fw - gw) / 2
    if cam:
        c0, c1 = cam[0] * fw, (cam[0] + cam[2]) * fw
        if x < c1 and x + gw > c0:  # overlaps the cam: try just left / just right of it, nearest to centre
            options = [o for o in (c0 - gw, c1) if 0 <= o <= fw - gw]
            if options:
                x = min(options, key=lambda o: abs(o - (fw - gw) / 2))
    top = (fh - gh) / 2
    return (x / fw, top / fh, gw / fw, gh / fh)


def parse_boxes(text: str) -> dict[str, Box]:
    """'caseoh_=0.7,0,0.3,0.3; kaicenat=0,0.6,0.25,0.4' -> {handle: (x, y, w, h)}. Raises ValueError."""
    out: dict[str, Box] = {}
    for part in (text or "").replace("\n", ";").split(";"):
        if not part.strip():
            continue
        handle, sep, nums = part.partition("=")
        try:
            vals = tuple(float(v) for v in nums.split(","))
        except ValueError:
            vals = ()
        if not sep or not handle.strip() or len(vals) != 4:
            raise ValueError(f"facecam_boxes: expected handle=x,y,w,h (fractions of the frame), got {part.strip()!r}")
        x, y, w, h = vals
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.001 and y + h <= 1.001):
            raise ValueError(f"facecam_boxes: {handle.strip()} box must lie inside the frame (0..1)")
        out[handle.strip().lstrip("@").lower()] = (x, y, w, h)
    return out


# ---- detection on real frames -----------------------------------------------------------
def sample_frames(pieces: list[Piece], ffmpeg: str = "ffmpeg", n: int = SAMPLES) -> list[np.ndarray]:
    total = sum(d for _, _, d in pieces)
    frames = []
    for i in range(n):
        t = (i + 0.5) / n * total
        for path, ss, dur in pieces:
            if t < dur or (path, ss, dur) == pieces[-1]:
                p = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{ss + min(t, dur - 0.1):.2f}", "-i", path,
                                    "-frames:v", "1", "-vf", f"scale={SAMPLE_W}:{SAMPLE_H}", "-f", "rawvideo",
                                    "-pix_fmt", "bgr24", "pipe:1"], capture_output=True)
                if len(p.stdout) == SAMPLE_W * SAMPLE_H * 3:
                    frames.append(np.frombuffer(p.stdout, np.uint8).reshape(SAMPLE_H, SAMPLE_W, 3))
                break
            t -= dur
    return frames


def detect_faces(frames: list[np.ndarray], min_score: float = MIN_SCORE) -> list[list[Box]]:
    import cv2  # imported here so the rest of the app runs without OpenCV
    det = cv2.FaceDetectorYN.create(str(MODEL), "", (SAMPLE_W, SAMPLE_H), min_score, 0.3, 50)
    out = []
    for img in frames:
        _, faces = det.detect(img)
        out.append([(float(f[0]) / SAMPLE_W, float(f[1]) / SAMPLE_H, float(f[2]) / SAMPLE_W, float(f[3]) / SAMPLE_H)
                    for f in (faces if faces is not None else []) if f[14] >= min_score and f[2] > 0 and f[3] > 0])
    return out


def find_facecam(pieces: list[Piece], fw: int, fh: int, ffmpeg: str = "ffmpeg") -> Box | None:
    """-> cam crop box (fractions of the frame), or None. A face counts as a facecam only if it stays in one
    place, keeps moving (not a poster or logo) and sits in a box with at least MIN_SIDES straight edges.
    Borderless (green-screen) cams therefore fall back to the blur layout unless pinned in facecam_boxes."""
    frames = sample_frames(pieces, ffmpeg)
    if not frames:
        return None
    for face in cam_candidates(detect_faces(frames)):
        motion = face_motion(frames, face)
        box, sides = cam_border(frames, face, cam_crop(face, fw, fh))
        log.info("face at %.2f,%.2f: motion %.1f, %d cam edges", face[0], face[1], motion, sides)
        if motion >= MIN_MOTION and sides >= MIN_SIDES:
            return box
    return None


def choose_layout(pieces: list[Piece], layout: str, game: str, handle: str, boxes: str = "",
                  ffmpeg: str = "ffmpeg") -> tuple[str, tuple[Box, Box] | None]:
    """-> (layout for render_clip, (cam box, game box) for the split layout).
    auto: split while a real game is on and a facecam is found, blur otherwise. split: split when a cam is found."""
    if layout not in ("auto", "split"):
        return layout, None
    if layout == "auto" and not is_game(game):
        return "blur", None
    sizes = {probe_size(p) for p, _, _ in pieces}
    fw, fh = sizes.pop() if len(sizes) == 1 and (0, 0) not in sizes else (1920, 1080)
    cam = parse_boxes(boxes).get(handle.lower()) if boxes else None
    if cam is None:
        try:
            cam = find_facecam(pieces, fw, fh, ffmpeg)
        except Exception:
            log.exception("facecam detection failed")
            cam = None
        if cam is None:
            log.info("%s: no steady facecam found, using the blur layout", handle)
            return "blur", None
    return "split", (cam, game_crop(cam, fw, fh))
