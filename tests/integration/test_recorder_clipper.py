import asyncio
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.clipper import ChunkIndex, make_thumb, probe_duration, render_clip
from app.detector import SR, audio_db_per_second, decode_audio
from app.recorder import Recorder

pytestmark = pytest.mark.integration


def fake_source(mp4: Path) -> list[str]:
    """Stands in for `streamlink --stdout`: emits the synthetic stream as MPEG-TS on stdout."""
    return ["ffmpeg", "-v", "error", "-i", str(mp4), "-c", "copy", "-f", "mpegts", "-"]


def probe_video(path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,codec_name", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


async def test_recorder_splits_stream_into_chunks(synthetic_mp4, tmp_path):
    rec = Recorder(tmp_path / "raw", fake_source(synthetic_mp4), chunk_seconds=30)
    await rec.start()
    await asyncio.wait_for(rec._task, 60)
    assert rec.finished and rec.last_returncode == 0
    chunks = rec.completed_chunks()
    assert len(chunks) == 4
    durs = [probe_duration(c) for c in chunks]
    assert all(28 <= d <= 31 for d in durs[:3]) and 8 <= durs[3] <= 12
    assert sum(durs) == pytest.approx(100, abs=2)


async def test_recorder_holds_back_the_chunk_still_being_written(tmp_path):
    rec = Recorder(tmp_path / "raw", ["sleep", "30"], chunk_seconds=30)
    (tmp_path / "raw").mkdir(exist_ok=True)
    for i in range(3):
        (tmp_path / "raw" / f"chunk_{i:05d}.ts").write_bytes(b"x")
    assert [p.name for p in rec.completed_chunks()] == ["chunk_00000.ts", "chunk_00001.ts"]
    rec.finished = True
    assert len(rec.completed_chunks()) == 3


async def test_recorder_stop_finalises_and_ends_promptly(synthetic_mp4, tmp_path):
    # -re plays in real time so the pipeline is still running when we stop it
    src = ["ffmpeg", "-v", "error", "-re", "-i", str(synthetic_mp4), "-c", "copy", "-f", "mpegts", "-"]
    rec = Recorder(tmp_path / "raw", src, chunk_seconds=30)
    await rec.start()
    await asyncio.sleep(10)  # ffmpeg probes ~5 s of input before it opens the first chunk
    await rec.stop()
    assert rec.finished
    chunks = rec.completed_chunks()
    assert len(chunks) == 1 and probe_duration(chunks[0]) > 2  # partial chunk is playable


async def test_recorder_restarts_failed_source_without_overwriting_chunks(synthetic_mp4, tmp_path):
    # forward-slash paths + LF newlines so the script also runs under Git Bash on Windows
    marker, mp4 = (tmp_path / "ran_once").as_posix(), Path(synthetic_mp4).as_posix()
    script = tmp_path / "flaky.sh"
    script.write_text(f"""#!/bin/bash
if [ ! -f {marker} ]; then touch {marker}; ffmpeg -v error -t 12 -i {mp4} -c copy -f mpegts - ; exit 3; fi
ffmpeg -v error -ss 30 -t 12 -i {mp4} -c copy -f mpegts -
""", newline="\n")
    script.chmod(0o755)
    rec = Recorder(tmp_path / "raw", [script.as_posix()], chunk_seconds=30, restart_delay=0.2)
    await rec.start()
    await asyncio.wait_for(rec._task, 60)
    assert rec.restarts == 1
    names = [p.name for p in rec.completed_chunks()]
    assert names == ["chunk_00000.ts", "chunk_00001.ts"]  # second run continued numbering


def test_decode_audio_and_burst_is_visible_in_levels(synthetic_mp4):
    samples = decode_audio(str(synthetic_mp4))
    assert abs(len(samples) / SR - 100) < 1.5
    db = audio_db_per_second(samples)
    assert db[20:55].mean() < -30 and db[62:67].mean() > db[20:55].mean() + 15
    assert decode_audio("/nonexistent.mp4").size == 0


@pytest.mark.parametrize("layout", ["blur", "crop"])
async def test_render_clip_across_chunk_boundaries(synthetic_mp4, tmp_path, layout):
    rec = Recorder(tmp_path / "raw", fake_source(synthetic_mp4), chunk_seconds=30)
    await rec.start()
    await asyncio.wait_for(rec._task, 60)
    idx = ChunkIndex()
    for c in rec.completed_chunks():
        idx.add(str(c), int(probe_duration(c)))
    pieces = idx.pieces(20, 80)  # crosses three chunks
    assert len(pieces) >= 3
    out = tmp_path / f"clip_{layout}.mp4"
    render_clip(pieces, out, layout)
    v = probe_video(out)
    assert (v["width"], v["height"], v["codec_name"]) == (1080, 1920, "h264")
    assert probe_duration(out) == pytest.approx(60, abs=1.5)
    # audio survived and the loud burst (at 60-68 -> 40-48 s into the clip) is audible
    db = audio_db_per_second(decode_audio(str(out)))
    assert db[42:47].mean() > db[5:35].mean() + 12
    thumb = tmp_path / "t.jpg"
    make_thumb(out, thumb)
    assert thumb.stat().st_size > 500


def test_render_clip_rejects_empty_and_reports_ffmpeg_errors(tmp_path):
    with pytest.raises(ValueError):
        render_clip([], tmp_path / "x.mp4")
    bad = tmp_path / "bad.ts"
    bad.write_bytes(b"not video")
    with pytest.raises(RuntimeError, match="ffmpeg render failed"):
        render_clip([(str(bad), 0, 5)], tmp_path / "y.mp4")


def test_render_handles_a_stream_that_changes_resolution(tmp_path):
    a, b = tmp_path / "a.ts", tmp_path / "b.ts"
    for path, size in ((a, "640x360"), (b, "320x180")):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=15:duration=6",
                        "-f", "lavfi", "-i", "anoisesrc=d=6:a=0.05", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", "-shortest", str(path)], check=True)
    out = tmp_path / "mixed.mp4"
    render_clip([(str(a), 1, 4), (str(b), 0, 4)], out, "blur")
    v = probe_video(out)
    assert (v["width"], v["height"]) == (1080, 1920)
    assert probe_duration(out) == pytest.approx(8, abs=0.8)
