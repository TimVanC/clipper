import numpy as np
import pytest

from app.detector import DetectorConfig, HypeDetector, audio_db_per_second
from tests.conftest import quiet_audio

pytestmark = pytest.mark.unit


def cfg(**kw) -> DetectorConfig:
    base = dict(warmup_s=30, pre_roll_s=40, post_roll_s=15)
    base.update(kw)
    return DetectorConfig(**base)


def audio_with_bursts(rng, n, bursts, boost=25.0):
    a = quiet_audio(rng, n)
    for s, e in bursts:
        a[s:e] += boost
    return a


def test_audio_db_per_second_levels():
    sr = 16000
    quiet = np.full(sr * 2, 0.01, dtype=np.float32)
    loud = np.full(sr * 2, 0.5, dtype=np.float32)
    db = audio_db_per_second(np.concatenate([quiet, loud]), sr)
    assert len(db) == 4
    assert db[0] == pytest.approx(-40, abs=0.1) and db[3] == pytest.approx(-6, abs=0.1)
    assert audio_db_per_second(np.zeros(10), sr).size == 0
    assert audio_db_per_second(np.zeros(sr), sr)[0] == -80.0  # silence floored


def test_flat_noise_produces_no_clips(rng):
    d = HypeDetector(cfg())
    d.ingest(quiet_audio(rng, 600))
    assert d.pop_ready(final=True) == []


def test_single_burst_gives_one_clip_with_pre_and_post_roll(rng):
    d = HypeDetector(cfg())
    d.ingest(audio_with_bursts(rng, 300, [(150, 158)]))
    out = d.pop_ready(final=True)
    assert len(out) == 1
    c = out[0]
    assert 149 <= c.peak_s <= 160
    assert c.audio_z > 3 and c.score >= 3.0
    # the 40 s of build-up before the onset is included, and 15 s after the peak
    assert c.start_s <= 150 - 40 + 3
    assert c.end_s == pytest.approx(c.peak_s + 15)
    assert c.end_s - c.start_s <= 90


def test_clip_is_held_back_until_post_roll_is_available(rng):
    d = HypeDetector(cfg())
    a = audio_with_bursts(rng, 300, [(150, 158)])
    d.ingest(a[:160])                       # only 2 s of post-burst data so far
    assert d.pop_ready() == []              # held: not enough post-roll yet
    d.ingest(a[160:240])
    out = d.pop_ready()
    assert len(out) == 1 and out[0].end_s <= d.seconds
    d.ingest(a[240:])
    assert d.pop_ready(final=True) == []    # never emitted twice


def test_warmup_suppresses_early_noise(rng):
    d = HypeDetector(cfg(warmup_s=60))
    d.ingest(audio_with_bursts(rng, 200, [(20, 26)]))
    assert d.pop_ready(final=True) == []


def test_min_separation_and_rate_limit(rng):
    close = HypeDetector(cfg(min_separation_s=60))
    close.ingest(audio_with_bursts(rng, 400, [(150, 156), (175, 181)]))
    assert len(close.pop_ready(final=True)) == 1

    far = HypeDetector(cfg(min_separation_s=60))
    far.ingest(audio_with_bursts(rng, 400, [(100, 106), (250, 256)]))
    assert len(far.pop_ready(final=True)) == 2

    capped = HypeDetector(cfg(max_clips_per_hour=1))
    capped.ingest(audio_with_bursts(rng, 400, [(100, 106), (250, 256)]))
    assert len(capped.pop_ready(final=True)) == 1


def test_chat_spike_alone_can_trigger_a_clip(rng):
    d = HypeDetector(cfg())
    chat = rng.poisson(2.0, 300).astype(float)
    chat[200:206] += 40
    d.ingest(quiet_audio(rng, 300), chat)
    out = d.pop_ready(final=True)
    assert len(out) == 1 and out[0].chat_z > 3 and 198 <= out[0].peak_s <= 210


def test_no_chat_means_chat_weight_ignored(rng):
    d = HypeDetector(cfg(chat_weight=5.0))
    d.ingest(audio_with_bursts(rng, 300, [(150, 158)]), chat=None)
    out = d.pop_ready(final=True)
    assert len(out) == 1  # would be diluted if the absent chat channel counted


def test_window_never_leaves_the_recorded_range(rng):
    d = HypeDetector(cfg(warmup_s=10))
    d.ingest(audio_with_bursts(rng, 120, [(40, 46)]))
    c = d.pop_ready(final=True)[0]
    assert c.start_s >= 0 and c.end_s <= 120 and c.end_s - c.start_s >= 20


def test_incremental_ingest_matches_single_shot(rng):
    a = audio_with_bursts(rng, 400, [(120, 126), (300, 306)])
    one = HypeDetector(cfg())
    one.ingest(a)
    expect = [round(c.peak_s) for c in one.pop_ready(final=True)]
    inc = HypeDetector(cfg())
    got = []
    for i in range(0, 400, 60):
        inc.ingest(a[i:i + 60])
        got += [round(c.peak_s) for c in inc.pop_ready()]
    got += [round(c.peak_s) for c in inc.pop_ready(final=True)]
    assert len(got) == len(expect) == 2
    assert all(abs(x - y) <= 3 for x, y in zip(sorted(got), sorted(expect)))


def test_clips_are_never_shorter_than_the_minimum(rng):
    d = HypeDetector(cfg())  # default min_clip_s = 61
    d.ingest(audio_with_bursts(rng, 600, [(150, 158), (400, 404)]))
    out = d.pop_ready(final=True)
    assert len(out) == 2
    for c in out:
        assert c.end_s - c.start_s >= 62            # 61 s + the render safety margin
        assert c.end_s == pytest.approx(c.peak_s + 15)  # extra length is build-up, the ending stays tight
    # peak right after the stream started: no room before it, so the clip runs on past the peak instead
    d = HypeDetector(cfg(warmup_s=10))
    d.ingest(audio_with_bursts(rng, 120, [(20, 26)]))
    c = d.pop_ready(final=True)[0]
    assert c.start_s == 0 and c.end_s - c.start_s >= 62
    # the minimum also wins over a smaller max_clip_s
    d = HypeDetector(cfg(max_clip_s=30))
    d.ingest(audio_with_bursts(rng, 300, [(150, 158)]))
    c = d.pop_ready(final=True)[0]
    assert c.end_s - c.start_s >= 62
