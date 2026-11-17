import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.config import Settings


def make_synthetic_stream(path: Path, seconds: int = 100, burst: tuple[int, int] = (60, 68)) -> Path:
    """A 'stream': 640x360 test pattern + quiet pink noise with a very loud burst (the 'hype moment')."""
    a, b = burst
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=15:duration={seconds}",
        "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:r=44100:a=0.02",
        "-af", f"volume=volume=25:enable='between(t,{a},{b})'",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "15", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest", str(path)], check=True)
    return path


@pytest.fixture(scope="session")
def synthetic_mp4(tmp_path_factory) -> Path:
    return make_synthetic_stream(tmp_path_factory.mktemp("synthetic") / "stream.mp4")


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(data_dir=tmp_path / "data")
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.chunk_seconds = 30
    s.warmup_s = 30
    s.delete_raw = False
    s.default_accounts = "spc_1"
    return s


@pytest.fixture
def rng():
    return np.random.default_rng(1234)


def quiet_audio(rng, n: int, level: float = -40.0, jitter: float = 0.8) -> np.ndarray:
    return level + rng.normal(0, jitter, n)
