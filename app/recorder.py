"""Records a live stream into fixed-length chunks: <source> | ffmpeg -f segment.

`source_cmd` is normally `streamlink --stdout <url> best` (works for Twitch, Kick and YouTube);
tests swap in a fake source that emits a synthetic stream on stdout.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import shlex
import shutil
import signal
import subprocess
from pathlib import Path

log = logging.getLogger("clipper.recorder")

WINDOWS = os.name == "nt"
# The pipeline (bash + source + ffmpeg) gets its own process group so stop() can signal all of it at once.
_GROUP_KW: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
                   else {"start_new_session": True})


@functools.lru_cache(maxsize=1)
def find_bash() -> str:
    """bash runs the source | ffmpeg pipeline. On Windows it is usually not on PATH in a plain terminal, but
    Git for Windows ships one; WSL's System32\\bash.exe is skipped (it runs Linux, without these tools)."""
    found = shutil.which("bash")
    if found and not (WINDOWS and found.lower().endswith(r"system32\bash.exe")):
        return found
    if WINDOWS:
        roots = []
        git = shutil.which("git")
        if git:
            roots.append(Path(git).resolve().parents[1])  # ...\Git\cmd\git.exe -> ...\Git
        roots += [Path(os.environ.get(v, "")) / "Git" for v in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA")]
        roots.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git")
        for root in roots:
            for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
                if cand.is_file():
                    return str(cand)
    raise RuntimeError("bash not found: install Git for Windows (git-scm.com) or put bash on PATH")


def _interrupt(proc: asyncio.subprocess.Process) -> bool:
    """Ask the whole pipeline to finish cleanly. Returns False if the request could not be delivered."""
    try:
        if WINDOWS:  # no killpg/SIGINT on Windows; Ctrl+Break reaches the process group via the console
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGINT)
        return True
    except ProcessLookupError:
        return True  # already gone
    except OSError:  # e.g. Windows service with no console attached
        return False


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        if WINDOWS:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def streamlink_source(streamlink: str, url: str, quality: str = "best") -> list[str]:
    # Twitch ad segments are filtered by streamlink itself, no flag needed.
    return [streamlink, "--stdout", "--quiet", "--retry-streams", "5", "--retry-max", "6",
            "--stream-timeout", "90", url, quality]


class Recorder:
    def __init__(self, out_dir: Path, source_cmd: list[str], chunk_seconds: int = 600,
                 ffmpeg: str = "ffmpeg", max_restarts: int = 5, restart_delay: float = 5.0):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.source_cmd, self.chunk_seconds, self.ffmpeg = source_cmd, chunk_seconds, ffmpeg
        self.max_restarts, self.restart_delay = max_restarts, restart_delay
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.finished = False
        self.restarts = 0
        self.last_returncode: int | None = None

    # ---- pipeline -------------------------------------------------
    def _chunk_files(self) -> list[Path]:
        return sorted(self.out_dir.glob("chunk_*.ts"))

    def _command(self) -> str:
        start = len(self._chunk_files())
        ff = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
              "-f", "segment", "-segment_time", str(self.chunk_seconds), "-reset_timestamps", "1",
              "-segment_format", "mpegts", "-segment_start_number", str(start),
              str(self.out_dir / "chunk_%05d.ts")]
        return f"{shlex.join(self.source_cmd)} | {shlex.join(ff)}"

    async def start(self) -> None:
        self._task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        try:
            while not self._stopping:
                self._proc = await asyncio.create_subprocess_exec(
                    find_bash(), "-o", "pipefail", "-c", self._command(),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    **_GROUP_KW)
                _, err = await self._proc.communicate()
                self.last_returncode = self._proc.returncode
                if self._stopping:
                    break
                if self.last_returncode == 0:  # source ended cleanly = stream is over
                    break
                log.warning("recorder exited rc=%s: %s", self.last_returncode, (err or b"")[-300:].decode(errors="replace"))
                if self.restarts >= self.max_restarts:
                    break
                self.restarts += 1
                await asyncio.sleep(self.restart_delay)
        finally:
            self.finished = True

    async def stop(self, timeout: float = 20.0) -> None:
        """Ask the whole pipeline to finish (SIGINT lets ffmpeg close its last segment cleanly)."""
        self._stopping = True
        proc = self._proc
        if proc and proc.returncode is None:
            graceful = _interrupt(proc)
            try:
                # Windows without a console can't deliver the interrupt: don't wait the full timeout for nothing
                wait = timeout if graceful else 0.5
                await asyncio.wait_for(asyncio.shield(self._task), wait) if self._task else None
            except asyncio.TimeoutError:
                await _kill_tree(proc)
        if self._task:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self.finished = True

    # ---- chunk access -------------------------------------------------
    def completed_chunks(self) -> list[Path]:
        """Chunks that ffmpeg has finished writing: everything but the newest, until the recorder ends."""
        files = [f for f in self._chunk_files() if f.stat().st_size > 0]
        if self.finished:
            return files
        return files[:-1]
