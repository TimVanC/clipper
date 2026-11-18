import json
import sqlite3
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from app import headline as hl
from app.chat import ChatLog
from app.config import Settings
from app.db import Database

pytestmark = pytest.mark.unit
S = hl.Segment


def test_local_headline_quotes_the_emphatic_line_said_at_the_peak():
    segs = [S(0, 4, "uh"), S(5, 9, "okay let's go to the store now"),
            S(40, 44, "Bro you cannot steal your neighbors cars in this game!"), S(50, 52, "yo")]
    assert hl.local_headline(segs, 42) == "“BRO YOU CANNOT STEAL YOUR NEIGHBORS CARS IN THIS GAME!”"
    assert hl.local_headline([S(10, 12, "wow"), S(30, 31, "ok")], 20) == ""  # nothing worth quoting
    long = hl.local_headline([S(0, 9, "this is a really long sentence that keeps going and going on and on forever")], 4)
    assert long.endswith("...”") and len(long) <= 66


def test_clean_strips_emoji_and_whitespace():
    assert hl.clean("  “NO WAY” \U0001F923\U0001F480  ") == "“NO WAY”"
    assert hl.clean("BEST WAY TO GET GUNS IN GTA 6‼️") == "BEST WAY TO GET GUNS IN GTA 6"


class FakeMessages:
    def __init__(self, reply, stop="end_turn"):
        self.reply, self.stop, self.calls = reply, stop, []

    async def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop, content=[SimpleNamespace(type="text", text=self.reply)])


def fake_client(reply, stop="end_turn"):
    m = FakeMessages(reply, stop)
    return SimpleNamespace(beta=SimpleNamespace(messages=m)), m


async def test_claude_headline_request_and_answer():
    client, m = fake_client(json.dumps({"kind": "theme", "headline": "Kai was SHOCKED by GTA 6 \U0001F633"}))
    segs = [S(0, 5, "yo what is this"), S(44, 48, "NO WAY they did that")]
    out = await hl.claude_headline(client, segs, 46, "Kai Cenat", "Grand Theft Auto VI", "GTA 6 DAY 1",
                                   ["LMAOOO", "W"])
    assert out == "Kai was SHOCKED by GTA 6"  # emoji removed
    kw = m.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["format"]["schema"]["required"] == ["kind", "headline"]
    prompt = kw["messages"][0]["content"]
    assert "Kai Cenat" in prompt and "NO WAY they did that  <- the hype peak" in prompt and "- LMAOOO" in prompt
    assert "No emoji" in kw["system"]


@pytest.mark.parametrize("reply,stop", [(json.dumps({"kind": "none", "headline": ""}), "end_turn"),
                                        ("not json", "end_turn"), ("", "refusal")])
async def test_claude_headline_gives_nothing_rather_than_something_wrong(reply, stop):
    client, _ = fake_client(reply, stop)
    assert await hl.claude_headline(client, [S(0, 2, "hi")], 1, "x") == ""


def test_render_png_fits_long_text_in_three_lines(tmp_path):
    w, h = hl.render_png("Kai was DISAPPOINTED he wasn't ABLE to walk into EVERY building inside GTA 6 "
                         "DESPITE waiting 13 YEARS for this game to finally come out", tmp_path / "a.png")
    assert w == 1080 and 100 < h < 330
    from PIL import Image
    img = np.array(Image.open(tmp_path / "a.png"))
    assert img[h // 2, 540, 3] > 200 and img[h // 2, 3, 3] == 0  # opaque box in the middle, clear at the edge


def test_apply_burns_the_headline_in_at_the_seam(tmp_path):
    base = tmp_path / "base.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=2",
                    "-f", "lavfi", "-i", "sine=d=2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(base)], check=True)
    out = tmp_path / "out.mp4"
    hl.apply(base, out, "“NO WAY”", 768)
    frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1", "-f", "rawvideo",
                            "-pix_fmt", "gray", "pipe:1"], capture_output=True).stdout
    img = np.frombuffer(frame, np.uint8).reshape(1920, 1080)
    white = img > 200
    assert white[700:840].sum() > 10_000 and not white[:600].any() and not white[950:].any()  # box at the seam only
    hl.apply(base, out, "", 768)  # no headline: a plain copy
    assert out.read_bytes() == base.read_bytes()


def test_chat_log_keeps_recent_message_text():
    log = ChatLog(keep_text_s=60)
    log.add(100.0, 1.0, "first")
    log.add(150.0, 2.0, "W")
    log.add(170.0, 1.0)  # weight only
    assert log.messages(140, 200) == ["W"] and log.total == 3
    log.add(300.0, 1.0, "late")
    assert log.messages(0, 400) == ["late"]  # older than keep_text_s dropped


def test_old_databases_get_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE clips(id INTEGER PRIMARY KEY, session_id INTEGER, platform TEXT, handle TEXT,"
                      " start_s REAL, end_s REAL, peak_s REAL, score REAL, audio_z REAL, chat_z REAL, path TEXT,"
                      " thumb TEXT, status TEXT NOT NULL DEFAULT 'rendering', caption TEXT NOT NULL DEFAULT '',"
                      " accounts TEXT NOT NULL DEFAULT '', post_id TEXT, post_error TEXT, created_at REAL,"
                      " decided_at REAL, posted_at REAL); INSERT INTO clips(id, handle) VALUES (7, 'x');")
    con.close()
    db = Database(path)
    clip = db.get_clip(7)
    assert clip["headline"] == "" and clip["seam"] == 0
    db.update_clip(7, headline="hi", seam=768)
    assert db.get_clip(7)["headline"] == "hi"


def test_headline_settings():
    s = Settings()
    assert s.headline_mode == "auto" and s.whisper_model == "small.en"
    s.update({"headline_mode": "local"})
    with pytest.raises(ValueError):
        s.update({"headline_mode": "sometimes"})
    assert Settings.from_env({"ANTHROPIC_API_KEY": "k", "WHISPER_MODEL": "base.en"}).whisper_model == "base.en"
