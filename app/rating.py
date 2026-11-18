"""A 0-10 rating per clip: how strongly the stream reacted at that moment, compared with the rest of the stream.

It is built from the detector's numbers: the peak hype score (audio and Twitch chat together, in robust
z-scores) plus a bonus when audio AND chat both spiked, which is a stronger sign than either alone. It knows
nothing about what happens on screen, so a loud but boring moment can still rate well; that is what the
review step is for.
"""
from __future__ import annotations

import math

SCALE = 4.5        # the score at which the rating reaches ~6.3; higher scores flatten towards 10
BOTH_BONUS = 0.25  # weight of "audio and chat both reacted"
LABELS = ((8.0, "great"), (6.5, "good"), (5.0, "ok"), (0.0, "weak"))


def clip_rating(score: float | None, audio_z: float | None = 0.0, chat_z: float | None = 0.0) -> float:
    s, a, c = (max(float(v or 0.0), 0.0) for v in (score, audio_z, chat_z))
    x = s + BOTH_BONUS * min(a, c)
    return round(10 * (1 - math.exp(-x / SCALE)), 1)


def rating_label(rating: float) -> str:
    return next(name for floor, name in LABELS if rating >= floor)
