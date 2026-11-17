"""Cut a window out of the recorded chunks and render it as a 1080x1920 vertical clip."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

Piece = tuple[str, float, float]  # (path, seek seconds inside file, duration)


@dataclass
class ChunkRef:
    path: str
    start: int   # offset on the session timeline (seconds)
    length: int  # whole seconds analysed; the timeline is defined by these


@dataclass
class ChunkIndex:
    chunks: list[ChunkRef] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.chunks[-1].start + self.chunks[-1].length if self.chunks else 0

    def add(self, path: str, seconds: int) -> ChunkRef:
        ref = ChunkRef(path, self.total, seconds)
        self.chunks.append(ref)
        return ref

    def pieces(self, start: float, end: float) -> list[Piece]:
        out: list[Piece] = []
        for c in self.chunks:
            s, e = max(start, c.start), min(end, c.start + c.length)
            if e - s > 0.05:
                out.append((c.path, s - c.start, e - s))
        return out


def probe_size(path: str, ffprobe: str = "ffprobe") -> tuple[int, int]:
    p = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0:s=x", str(path)], capture_output=True, text=True)
    try:
        w, h = p.stdout.strip().split("x")
        return int(w), int(h)
    except ValueError:
        return (0, 0)


def build_filtergraph(n_pieces: int, layout: str, norm: tuple[int, int] | None = None) -> str:
    """norm=None: all pieces already share a size (the normal case, no re-scaling needed).
    norm=(w, h): letterbox every piece to that size first (stream changed resolution mid-way)."""
    parts = []
    for i in range(n_pieces):
        if norm:
            w, h = norm
            v = (f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]")
        else:
            v = f"[{i}:v]setsar=1,fps=30[v{i}]"
        parts.append(v + f";[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
    joined = "".join(f"[v{i}][a{i}]" for i in range(n_pieces))
    parts.append(f"{joined}concat=n={n_pieces}:v=1:a=1[cv][aout]")
    if layout == "crop":
        parts.append("[cv]scale=-2:1920,crop=1080:1920,format=yuv420p[vout]")
    else:  # blur: sharp 16:9 stream centred over a blurred, zoomed copy (the standard clip look)
        parts.append(
            "[cv]split=2[bg][fg];"
            # blur at tiny resolution then scale up: looks identical, costs ~nothing
            "[bg]scale=108:192:force_original_aspect_ratio=increase,crop=108:192,boxblur=3:2,"
            "scale=1080:1920:flags=bilinear[bgb];"
            "[fg]scale=1080:-2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]"
        )
    return ";".join(parts)


def render_clip(pieces: list[Piece], out_path: str | Path, layout: str = "blur", ffmpeg: str = "ffmpeg") -> None:
    if not pieces:
        raise ValueError("no media pieces for this window")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sizes = {probe_size(p) for p, _, _ in pieces}
    norm = None if len(sizes) == 1 and (0, 0) not in sizes else (1920, 1080)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for path, ss, dur in pieces:
        cmd += ["-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", path]
    cmd += ["-filter_complex", build_filtergraph(len(pieces), layout, norm),
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_path)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg render failed: {p.stderr[-500:]}")


def make_thumb(video: str | Path, out_path: str | Path, ffmpeg: str = "ffmpeg", at: float = 3.0) -> None:
    p = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=360:-2", str(out_path)], capture_output=True, text=True)
    if p.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"thumbnail failed: {p.stderr[-300:]}")


def make_preview(video: str | Path, out_path: str | Path, ffmpeg: str = "ffmpeg") -> None:
    """Small 360x640 copy (~6 MB for 90 s) so the Discord message can carry a playable preview."""
    p = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
                        "-vf", "scale=360:-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
                        "-maxrate", "550k", "-bufsize", "1100k", "-c:a", "aac", "-b:a", "48k", "-ac", "1",
                        "-movflags", "+faststart", str(out_path)], capture_output=True, text=True)
    if p.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"preview failed: {p.stderr[-300:]}")


def probe_duration(path: str | Path, ffprobe: str = "ffprobe") -> float:
    p = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0
