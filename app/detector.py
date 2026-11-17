"""Hype detection: audio loudness + chat activity -> robust z-scores -> clip windows.

All times are 'stream seconds' on one continuous integer timeline built from the recorded chunks.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

import numpy as np

SR = 16000


@dataclass
class DetectorConfig:
    pre_roll_s: float = 40.0
    post_roll_s: float = 15.0
    max_clip_s: float = 90.0
    min_clip_s: float = 20.0
    z_threshold: float = 3.0
    min_separation_s: float = 60.0
    max_clips_per_hour: int = 12
    warmup_s: float = 60.0
    baseline_s: int = 1800
    audio_weight: float = 1.0
    chat_weight: float = 1.5
    onset_z: float = 1.5
    audio_smooth_s: int = 3
    chat_smooth_s: int = 5
    audio_floor_db: float = 1.5
    chat_floor: float = 1.0

    @classmethod
    def from_settings(cls, s) -> "DetectorConfig":
        return cls(pre_roll_s=s.pre_roll_s, post_roll_s=s.post_roll_s, max_clip_s=s.max_clip_s,
                   min_clip_s=s.min_clip_s, z_threshold=s.z_threshold, min_separation_s=s.min_separation_s,
                   max_clips_per_hour=s.max_clips_per_hour, warmup_s=s.warmup_s,
                   audio_weight=s.audio_weight, chat_weight=s.chat_weight)


@dataclass
class Candidate:
    peak_s: float
    start_s: float
    end_s: float
    score: float
    audio_z: float
    chat_z: float


def decode_audio(path: str, ffmpeg: str = "ffmpeg") -> np.ndarray:
    """Decode to mono 16 kHz float32 in [-1, 1]. Empty array if the file has no audio / is unreadable."""
    p = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True)
    if p.returncode != 0 or not p.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def audio_db_per_second(samples: np.ndarray, sr: int = SR) -> np.ndarray:
    """RMS loudness in dBFS for each whole second (silence floored at -80 dB)."""
    n = len(samples) // sr
    if n == 0:
        return np.zeros(0)
    blocks = samples[: n * sr].reshape(n, sr)
    rms = np.sqrt(np.mean(blocks.astype(np.float64) ** 2, axis=1))
    return np.maximum(20 * np.log10(rms + 1e-9), -80.0)


def _smooth(x: np.ndarray, k: int, mean: bool) -> np.ndarray:
    if k <= 1 or len(x) == 0:
        return x.astype(np.float64)
    pad = k // 2
    xp = np.pad(x.astype(np.float64), (pad, k - 1 - pad), mode="edge")
    out = np.convolve(xp, np.ones(k), mode="valid")
    return out / k if mean else out


def _robust_z(series: np.ndarray, a: int, b: int, window: int, floor: float, block: int = 30) -> np.ndarray:
    """z-score of series[a:b] against the median/MAD of the preceding `window` seconds (so a burst
    can't hide in its own baseline). Early on, falls back to the data seen so far."""
    z = np.zeros(max(0, b - a))
    for k in range(a, b, block):
        e = min(b, k + block)
        lo = max(0, k - window)
        base = series[lo:k] if k - lo >= block else series[lo:e]
        med = np.median(base)
        scale = max(1.4826 * np.median(np.abs(base - med)), floor)
        z[k - a:e - a] = (series[k:e] - med) / scale
    return np.clip(z, -2.0, None)


class HypeDetector:
    def __init__(self, cfg: DetectorConfig):
        self.cfg = cfg
        self._audio = np.zeros(0)
        self._chat = np.zeros(0)
        self._chat_enabled = False
        self._scanned = 0
        self._emitted: list[float] = []  # peak times already turned into clips

    @property
    def seconds(self) -> int:
        return len(self._audio)

    def ingest(self, audio_db: np.ndarray, chat: np.ndarray | None = None) -> int:
        """Append one chunk of per-second signals. Returns the chunk length in seconds."""
        n = len(audio_db)
        if chat is None:
            chat_arr = np.zeros(n)
        else:
            self._chat_enabled = True
            chat_arr = np.zeros(n)
            m = min(n, len(chat))
            chat_arr[:m] = chat[:m]
        self._audio = np.concatenate([self._audio, np.asarray(audio_db, dtype=np.float64)])
        self._chat = np.concatenate([self._chat, chat_arr])
        return n

    def _scores(self, a: int, b: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        c = self.cfg
        audio_s = _smooth(self._audio, c.audio_smooth_s, mean=True)
        chat_s = _smooth(self._chat, c.chat_smooth_s, mean=False)
        za = _robust_z(audio_s, a, b, c.baseline_s, c.audio_floor_db)
        zc = _robust_z(chat_s, a, b, c.baseline_s, c.chat_floor) if self._chat_enabled else np.zeros(b - a)
        wa = c.audio_weight
        wc = c.chat_weight if self._chat_enabled else 0.0
        norm = np.sqrt(wa ** 2 + wc ** 2) or 1.0
        return (wa * za + wc * zc) / norm, za, zc

    def pop_ready(self, final: bool = False) -> list[Candidate]:
        """Return new clip candidates. Unless `final`, the last post_roll seconds are held back so a
        clip always has its full post-roll available."""
        c = self.cfg
        n = self.seconds
        scan_to = n if final else int(n - c.post_roll_s)
        scan_from = self._scanned
        if scan_to <= scan_from:
            return []
        self._scanned = scan_to

        look = 60
        lo = max(0, scan_from - look - 5)
        hi = min(n, scan_to + 5)
        score, za, zc = self._scores(lo, hi)

        def at(t: int) -> float:
            return float(score[t - lo])

        half = 5
        found: list[tuple[float, int]] = []
        for t in range(scan_from, scan_to):
            if t < c.warmup_s:
                continue
            s = at(t)
            if s < c.z_threshold:
                continue
            w0, w1 = max(lo, t - half), min(hi, t + half + 1)
            window = score[w0 - lo:w1 - lo]
            if s >= window.max() and int(np.argmax(window)) + w0 == t:  # first max wins ties
                found.append((s, t))

        accepted: list[tuple[float, int]] = []
        for s, t in sorted(found, reverse=True):
            if any(abs(t - p) < c.min_separation_s for p in self._emitted):
                continue
            if any(abs(t - p) < c.min_separation_s for _, p in accepted):
                continue
            recent = [p for p in self._emitted if p > t - 3600] + [p for _, p in accepted if p > t - 3600]
            if len(recent) >= c.max_clips_per_hour:
                continue
            accepted.append((s, t))

        out: list[Candidate] = []
        for s, t in sorted(accepted, key=lambda x: x[1]):
            onset = t
            while onset > max(0, t - 60) and onset - 1 >= lo and at(onset - 1) >= c.onset_z:
                onset -= 1
            start = max(0.0, onset - c.pre_roll_s)
            end = min(float(n), t + c.post_roll_s)
            if end - start > c.max_clip_s:
                start = max(0.0, end - c.max_clip_s)
            if end - start < c.min_clip_s:
                end = min(float(n), start + c.min_clip_s)
                if end - start < c.min_clip_s:
                    start = max(0.0, end - c.min_clip_s)
            out.append(Candidate(float(t), start, end, s, float(za[t - lo]), float(zc[t - lo])))
            self._emitted.append(float(t))
        return out
