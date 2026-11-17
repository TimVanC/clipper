"""Uses the REAL streamlink binary (no internet needed): serve an HLS stream from localhost and record it
with the exact command line the app builds for production. Guards against streamlink flag drift."""
import asyncio
import functools
import http.server
import re
import shutil
import subprocess
import threading

import pytest

from app.clipper import probe_duration
from app.recorder import Recorder, streamlink_source

pytestmark = pytest.mark.integration
needs_streamlink = pytest.mark.skipif(shutil.which("streamlink") is None, reason="streamlink not installed")


@needs_streamlink
def test_every_flag_we_emit_exists_in_installed_streamlink():
    help_text = subprocess.run(["streamlink", "--help"], capture_output=True, text=True).stdout
    flags = [a for a in streamlink_source("streamlink", "https://twitch.tv/x") if a.startswith("--")]
    assert flags
    missing = [f for f in flags if not re.search(rf"(^|\s){re.escape(f)}(\s|,|=|$)", help_text, re.M)]
    assert missing == []


@needs_streamlink
async def test_streamlink_to_chunks_over_real_hls(synthetic_mp4, tmp_path, monkeypatch):
    hls = tmp_path / "hls"
    hls.mkdir()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(synthetic_mp4), "-c", "copy", "-f", "hls",
                    "-hls_time", "4", "-hls_playlist_type", "vod", str(hls / "index.m3u8")], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(hls))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    try:
        url = f"hls://http://127.0.0.1:{srv.server_address[1]}/index.m3u8"
        rec = Recorder(tmp_path / "raw", streamlink_source("streamlink", url), chunk_seconds=30)
        await rec.start()
        await asyncio.wait_for(rec._task, 90)
    finally:
        srv.shutdown()
    assert rec.last_returncode == 0
    chunks = rec.completed_chunks()
    assert len(chunks) >= 3
    assert sum(probe_duration(c) for c in chunks) == pytest.approx(100, abs=6)
