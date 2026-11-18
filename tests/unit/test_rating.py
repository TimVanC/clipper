import time

import pytest

from app.db import Database
from app.rating import clip_rating, rating_label
from app.web import _public

pytestmark = pytest.mark.unit


def test_rating_grows_with_the_reaction_and_rewards_audio_and_chat_together():
    assert clip_rating(0, 0, 0) == 0.0 and clip_rating(None, None, None) == 0.0
    assert clip_rating(-2, -1, -1) == 0.0
    weak, ok, strong, huge = (clip_rating(s) for s in (2.0, 3.1, 6.7, 30))
    assert weak < ok < strong < huge <= 10.0
    assert clip_rating(4.0, 4.0, 4.0) > clip_rating(4.0, 8.0, 0.0)  # both spiking beats one loud signal
    assert [rating_label(r) for r in (9.0, 7.0, 5.5, 1.0)] == ["great", "good", "ok", "weak"]
    # the clips from the GTA VI countdown test (score ~3.1-3.6) rate "ok"; CaseOh's big moment (9.1) "great"
    assert rating_label(clip_rating(3.1, 2.0, 3.7)) == "ok"
    assert rating_label(clip_rating(9.1, 6.0, 9.9)) == "great"


def test_public_clip_carries_the_rating():
    c = _public({"id": 1, "path": "x.mp4", "thumb": "", "score": 6.7, "audio_z": 3.0, "chat_z": 8.3})
    assert c["rating"] == clip_rating(6.7, 3.0, 8.3) == 8.1 and c["rating_label"] == "great" and "path" not in c


def test_find_bash_falls_back_to_git_for_windows(tmp_path, monkeypatch):
    import app.recorder as rec
    git_root = tmp_path / "Git"
    (git_root / "cmd").mkdir(parents=True)
    (git_root / "bin").mkdir()
    (git_root / "cmd" / "git.exe").write_text("")
    (git_root / "bin" / "bash.exe").write_text("")
    monkeypatch.setattr(rec, "WINDOWS", True)
    for env in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        monkeypatch.setenv(env, str(tmp_path / "nowhere"))
    which = {"bash": None, "git": str(git_root / "cmd" / "git.exe")}
    monkeypatch.setattr(rec.shutil, "which", lambda n: which[n])
    rec.find_bash.cache_clear()
    try:
        assert rec.find_bash() == str(git_root / "bin" / "bash.exe")      # plain terminal: bash not on PATH
        rec.find_bash.cache_clear()
        which["bash"] = r"C:\Windows\System32\bash.exe"                      # WSL's bash is skipped
        assert rec.find_bash() == str(git_root / "bin" / "bash.exe")
        rec.find_bash.cache_clear()
        which["bash"], which["git"] = None, None
        (git_root / "bin" / "bash.exe").unlink()
        with pytest.raises(RuntimeError, match="Git for Windows"):
            rec.find_bash()
    finally:
        rec.find_bash.cache_clear()


def test_posts_cut_off_by_a_restart_are_failed_not_retried(tmp_path):
    db = Database(tmp_path / "c.db")
    ids = [db.add_clip(1, "twitch", "x", 0, 61, 50, 3.0, 2.0, 1.0) for _ in range(3)]
    db.update_clip(ids[0], status="posting")
    db.update_clip(ids[1], status="posted", posted_at=time.time())
    db.update_clip(ids[2], status="pending")
    assert db.fail_interrupted_posts() == [ids[0]]
    stuck = db.get_clip(ids[0])
    assert stuck["status"] == "failed" and "check your profiles" in stuck["post_error"]
    assert [db.get_clip(i)["status"] for i in ids[1:]] == ["posted", "pending"]
    assert db.fail_interrupted_posts() == []
    cut = db.add_clip(1, "twitch", "x", 0, 61, 50, 3.0, 2.0, 1.0)  # new clips start as 'rendering'
    assert db.fail_interrupted_renders() == [cut] and db.get_clip(cut)["status"] == "failed"
