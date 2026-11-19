"""Records a live stream into short chunks: <source> piped into `ffmpeg -f segment`.

`source_cmd` is normally `streamlink --stdout <url> best` (works for Twitch, Kick and YouTube);
tests swap in a fake source that emits a synthetic stream on stdout.

The source and ffmpeg run as two processes joined by a pipe that this module owns (no shell in between), so
stop() can end both for certain: killing the source closes ffmpeg's input, ffmpeg finishes the chunk it is
writing and exits. On Windows, contain_children() also puts the app in a job object that takes every child
process down with it, so a crashed or killed app can't leave recorders writing to disk.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import shutil
import signal
import subprocess
from pathlib import Path

log = logging.getLogger("clipper.recorder")

WINDOWS = os.name == "nt"
# own process group: a Ctrl-C in the app's console doesn't hit the recorders before stop() can end them cleanly
_GROUP_KW: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
                   else {"start_new_session": True})
_job_handle = None  # keeps the Windows job object open for the app's lifetime


def contain_children() -> bool:
    """Windows: put this process in a job object that kills all its child processes when the app exits, however
    it exits (crash, Task Manager, a closed terminal). Children made later are in the job too. Returns whether
    it is in place; elsewhere stop() / the process group does the job."""
    global _job_handle
    if not WINDOWS or _job_handle is not None:
        return _job_handle is not None
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes",
                                                   "OtherBytes")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        log.warning("couldn't create a job object (error %s): recorders may outlive a crashed app",
                    ctypes.get_last_error())
        return False
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # extended limits
            and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())):
        log.warning("couldn't set up the job object (error %s): recorders may outlive a crashed app",
                    ctypes.get_last_error())
        return False
    _job_handle = job
    return True


@functools.lru_cache(maxsize=1)
def find_bash() -> str:
    """Only for a source that is a shell script (tests). On Windows bash is usually not on PATH in a plain
    terminal, but Git for Windows ships one; WSL's System32\\bash.exe is skipped (it runs Linux)."""
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


async def _kill_tree(proc: asyncio.subprocess.Process | None) -> None:
    """Kill a process and everything it started (streamlink.exe runs its real work in a child python)."""
    if proc is None or proc.returncode is not None:
        return
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
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        log.warning("process %s didn't exit after being killed", proc.pid)


def streamlink_source(streamlink: str, url: str, quality: str = "best") -> list[str]:
    # Twitch ad segments are filtered by streamlink itself, no flag needed.
    return [streamlink, "--stdout", "--quiet", "--retry-streams", "5", "--retry-max", "6",
            "--stream-timeout", "90", url, quality]


class Recorder:
    def __init__(self, out_dir: Path, source_cmd: list[str], chunk_seconds: int = 30,
                 ffmpeg: str = "ffmpeg", max_restarts: int = 5, restart_delay: float = 5.0):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.source_cmd, self.chunk_seconds, self.ffmpeg = source_cmd, chunk_seconds, ffmpeg
        self.max_restarts, self.restart_delay = max_restarts, restart_delay
        self._src: asyncio.subprocess.Process | None = None
        self._ff: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.finished = False
        self.restarts = 0
        self.last_returncode: int | None = None

    # ---- pipeline -------------------------------------------------
    def _chunk_files(self) -> list[Path]:
        return sorted(self.out_dir.glob("chunk_*.ts"))

    def _next_number(self) -> int:
        """Continue the numbering after a restart (older chunks may already be deleted)."""
        files = self._chunk_files()
        return int(files[-1].stem.split("_")[1]) + 1 if files else 0

    def _source(self) -> list[str]:
        cmd = list(self.source_cmd)
        if WINDOWS and cmd and cmd[0].lower().endswith(".sh"):  # scripts can't be executed directly on Windows
            cmd = [find_bash(), *cmd]
        return cmd

    def _ffmpeg(self) -> list[str]:
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
                "-f", "segment", "-segment_time", str(self.chunk_seconds), "-reset_timestamps", "1",
                "-segment_format", "mpegts", "-segment_start_number", str(self._next_number()),
                str(self.out_dir / "chunk_%05d.ts")]

    async def start(self) -> None:
        self._task = asyncio.create_task(self._supervise())

    async def _run_once(self) -> tuple[int, bytes]:
        read_end, write_end = os.pipe()
        try:
            self._src = await asyncio.create_subprocess_exec(
                *self._source(), stdin=asyncio.subprocess.DEVNULL, stdout=write_end,
                stderr=asyncio.subprocess.PIPE, **_GROUP_KW)
            self._ff = await asyncio.create_subprocess_exec(
                *self._ffmpeg(), stdin=read_end, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, **_GROUP_KW)
        except OSError as e:
            await _kill_tree(self._src)
            return 127, str(e).encode()
        finally:  # the children hold their own copies: ffmpeg sees EOF when the source exits
            os.close(read_end)
            os.close(write_end)
        (_, src_err), (_, ff_err) = await asyncio.gather(self._src.communicate(), self._ff.communicate())
        rc = self._src.returncode or self._ff.returncode or 0
        return rc, (src_err or b"") + (ff_err or b"")

    async def _supervise(self) -> None:
        try:
            while not self._stopping:
                rc, err = await self._run_once()
                self.last_returncode = rc
                if self._stopping:
                    break
                if rc == 0:  # source ended cleanly = stream is over
                    break
                log.warning("recorder exited rc=%s: %s", rc, err[-300:].decode(errors="replace"))
                if self.restarts >= self.max_restarts:
                    break
                self.restarts += 1
                await asyncio.sleep(self.restart_delay)
        finally:
            await _kill_tree(self._src)
            await _kill_tree(self._ff)
            self.finished = True

    async def stop(self, timeout: float = 20.0) -> None:
        """End the recording for certain: kill the source, let ffmpeg close its last chunk, kill it if it hangs."""
        self._stopping = True
        await _kill_tree(self._src)
        ff = self._ff
        if ff and ff.returncode is None:
            try:
                await asyncio.wait_for(ff.wait(), timeout)
            except asyncio.TimeoutError:
                await _kill_tree(ff)
        if self._task:
            try:
                await asyncio.wait_for(self._task, 15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self.finished = True

    # ---- chunk access -------------------------------------------------
    def completed_chunks(self) -> list[Path]:
        """Chunks that ffmpeg has finished writing: everything but the newest, until the recorder ends."""
        files = []
        for f in self._chunk_files():
            try:
                if f.stat().st_size > 0:
                    files.append(f)
            except FileNotFoundError:  # deleted by the buffer cleanup meanwhile
                pass
        if self.finished:
            return files
        return files[:-1]
