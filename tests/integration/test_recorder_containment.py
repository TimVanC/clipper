"""A killed app must not leave recorder processes behind (they kept writing chunks for hours once)."""
import os
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.name != "nt", reason="the job object is Windows-only; elsewhere killpg")]

CHILD = """
import subprocess, sys, time
from app.recorder import contain_children
assert contain_children()
p = subprocess.Popen(["ping", "-n", "120", "127.0.0.1"], stdout=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
print(p.pid, flush=True)
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def test_children_die_with_the_app():
    app = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE, text=True,
                           cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    child = int(app.stdout.readline())
    assert _alive(child)
    subprocess.run(["taskkill", "/F", "/PID", str(app.pid)], capture_output=True)  # no tree: like a crash
    deadline = time.time() + 10
    while _alive(child) and time.time() < deadline:
        time.sleep(0.2)
    assert not _alive(child)
