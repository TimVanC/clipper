"""The on-screen headline: the short line at the seam between cam and game on TikTok-style clips
("YOU CAN'T STEAL YOUR NEIGHBORS CARS IN GTA6" / "Kai was DISAPPOINTED he wasn't ABLE to...").

1. transcribe(): what was said in the clip, with OpenAI's open Whisper model run locally (faster-whisper).
2. A headline from that transcript and the chat around the peak:
   - claude_headline(): Claude picks the real quote or writes the theme line, grounded in the transcript;
   - local_headline(): no API key, so the most emphatic line said around the peak, in quotes.
3. render_png() draws it (white rounded box, black bold text, colour emoji) and apply() burns it into the
   clip at the seam. The text-free render is kept, so an edited headline can be re-applied before posting.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("clipper.headline")

OUT_W, OUT_H = 1080, 1920
MAX_CHARS = 90
CLAUDE_MODEL = "claude-opus-5-5"


@dataclass
class Segment:
    start: float  # seconds into the clip
    end: float
    text: str


# ---- 1. transcription -------------------------------------------------------------------------
_models: dict[str, object] = {}
_lock = threading.Lock()


def load_audio(video: str | Path, ffmpeg: str = "ffmpeg") -> "np.ndarray":
    """16 kHz mono float32, decoded by ffmpeg (faster-whisper's own PyAV reader breaks on some PyAV versions)."""
    import numpy as np
    p = subprocess.run([ffmpeg, "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "f32le", "pipe:1"], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"audio decode failed: {p.stderr[-200:]!r}")
    return np.frombuffer(p.stdout, np.float32).copy()


def transcribe(video: str | Path, model_name: str = "small.en", ffmpeg: str = "ffmpeg") -> list[Segment]:
    """Speech in the clip. The model (downloaded once, ~0.5 GB for small.en) is loaded on first use and
    kept; transcriptions run one at a time so several renders don't each load their own copy."""
    # Anaconda's numpy and CTranslate2 (under faster-whisper) each ship an OpenMP runtime; on Windows the second
    # one aborts the process unless allowed. Must be set before CTranslate2 loads.
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    from faster_whisper import WhisperModel  # imported here: the rest of the app runs without it
    audio = load_audio(video, ffmpeg)
    with _lock:
        model = _models.get(model_name)
        if model is None:
            log.info("loading Whisper model %s (first use downloads it)", model_name)
            model = _models[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio, beam_size=5, vad_filter=True, condition_on_previous_text=False)
        return [Segment(float(s.start), float(s.end), s.text.strip()) for s in segments if s.text.strip()]


def transcript_text(segments: list[Segment], peak_s: float | None = None) -> str:
    lines = []
    for s in segments:
        mark = "  <- the hype peak" if peak_s is not None and s.start <= peak_s <= s.end + 2 else ""
        lines.append(f"[{s.start:5.1f}-{s.end:5.1f}s] {s.text}{mark}")
    return "\n".join(lines)


# ---- 2a. free headline: the most emphatic line said around the peak ----------------------------------
_FILLER = re.compile(r"^(uh+|um+|bro|yo|like|ok(ay)?|oh|ah+|man|dude)[,.!?\s]*$", re.I)


def local_headline(segments: list[Segment], peak_s: float) -> str:
    best, best_score = "", 0.0
    for s in segments:
        text = re.sub(r"\s+", " ", s.text).strip(" -")
        words = text.split()
        if len(words) < 3 or _FILLER.match(text):
            continue
        dist = 0.0 if s.start - 3 <= peak_s <= s.end + 3 else min(abs(peak_s - s.start), abs(peak_s - s.end))
        score = (3.0 / (1.0 + dist / 5.0)                              # said right around the moment
                 + 0.6 * text.count("!") + 0.4 * text.count("?")
                 + 0.15 * min(len(words), 12)                          # says something, not just a shout
                 - (0.5 if len(words) > 18 else 0.0))
        if score > best_score:
            best, best_score = text, score
    if not best:
        return ""
    if len(best) > 60:  # keep the punchy part: cut at a word boundary
        best = best[:60].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return f"\u201C{best.upper()}\u201D"


# ---- 2b. Claude headline ---------------------------------------------------------------------------
SYSTEM = """You write the one-line headline that sits on TikTok and YouTube Shorts clips of livestreams, \
between the streamer's camera and the gameplay. It is the first thing a viewer reads, so it must make them \
stay, and it must be true to the clip.

Two kinds work:
- QUOTE: the funniest, wildest or most telling thing the streamer actually said, in curly quotes, usually \
in capitals: "YOU CAN'T STEAL YOUR NEIGHBORS CARS IN GTA6"
- THEME: what happened, written like a viewer telling a friend, a few words in CAPITALS for emphasis: \
Kai was DISAPPOINTED he wasn't ABLE to walk into EVERY building inside GTA 6...

Rules:
- Use only what the transcript and chat show. A quote must be words the streamer really said (you may fix \
spelling, punctuation and filler words, and shorten it). Never invent events, names or numbers.
- The transcript comes from speech recognition over game audio and may contain mistakes or lines spoken by \
game characters; ignore anything that doesn't make sense.
- At most 90 characters. No emoji (they are burnt into the video and would not match the viewer's phone), \
no hashtags. Call Grand Theft Auto VI "GTA 6".
- No slurs, no sexual content, nothing targeting a real person's identity.
- If nothing in the transcript makes a good, true headline, return an empty headline."""

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["quote", "theme", "none"]},
        "headline": {"type": "string"},
    },
    "required": ["kind", "headline"],
    "additionalProperties": False,
}


def claude_prompt(segments: list[Segment], peak_s: float, streamer: str, game: str, title: str,
                  chat: list[str]) -> str:
    chat_lines = "\n".join(f"- {m}" for m in chat[-40:]) or "(no chat captured)"
    return (f"Streamer: {streamer}\nGame / category: {game or 'unknown'}\nStream title: {title or 'unknown'}\n"
            f"The hype peak is at {peak_s:.0f}s into this {segments[-1].end if segments else 60:.0f}s clip.\n\n"
            f"Transcript of the clip:\n{transcript_text(segments, peak_s) or '(no speech recognised)'}\n\n"
            f"Twitch chat right around the peak:\n{chat_lines}\n\nWrite the headline.")


async def claude_headline(client, segments: list[Segment], peak_s: float, streamer: str, game: str = "",
                          title: str = "", chat: list[str] | None = None, model: str = CLAUDE_MODEL) -> str:
    """client: anthropic.AsyncAnthropic. Returns '' when there is nothing good (or on a refusal)."""
    response = await client.beta.messages.create(
        model=model,
        max_tokens=2000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # a declined request is retried on the fallback model in the same call
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": claude_prompt(segments, peak_s, streamer, game, title, chat or [])}],
    )
    if response.stop_reason == "refusal":
        log.info("headline request declined for %s", streamer)
        return ""
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        log.warning("headline response was not JSON: %r", text[:200])
        return ""
    headline = clean(str(data.get("headline", "")))
    if data.get("kind") == "none" or not headline:
        return ""
    return headline[:MAX_CHARS + 10]


def clean(text: str) -> str:
    """No emoji in burnt-in text: they'd be drawn in this computer's emoji style, not the viewer's phone's."""
    return re.sub(r"\s+", " ", _EMOJI.sub("", text or "")).strip()


# ---- 3. drawing and burning in ----------------------------------------------------------------------
FONT_CANDIDATES = {
    "text": ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    "emoji": ["C:/Windows/Fonts/seguiemj.ttf", "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"],
}
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u203C\u2049\u2122\u2139"
                    "\u2194-\u21AA\u231A-\u23FF\u25AA-\u25FE\u3030\u303D\u3297\u3299]+")


def _font(kind: str, size: int):
    from PIL import ImageFont
    for path in FONT_CANDIDATES[kind]:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:  # bitmap colour fonts (Noto) only come in one size
                try:
                    return ImageFont.truetype(path, 109)
                except OSError:
                    continue
    return ImageFont.load_default(size) if kind == "text" else None


def _runs(text: str) -> list[tuple[str, bool]]:
    """'wow 😳🤯' -> [('wow ', False), ('😳🤯', True)]"""
    out, pos = [], 0
    for m in _EMOJI.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out


def _measure(draw, text: str, font, efont, size: int) -> float:
    w = 0.0
    for run, is_emoji in _runs(text):
        if is_emoji and efont is not None:
            w += len([c for c in run if c not in "\uFE0F\u200D"]) * size * 1.15
        else:
            w += draw.textlength(run, font=font)
    return w


def _wrap(draw, text: str, font, efont, size: int, max_w: float) -> list[str]:
    words: list[str] = []
    for word in text.split():  # emoji stick to the word before them, never alone on a line
        if words and not _EMOJI.sub("", word).strip(".!?…"):
            words[-1] += " " + word
        else:
            words.append(word)
    lines, cur = [], ""
    for word in words:
        test = f"{cur} {word}".strip()
        if cur and _measure(draw, test, font, efont, size) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


def render_png(text: str, out: str | Path, width: int = OUT_W, style: str = "box") -> tuple[int, int]:
    """The headline as a transparent PNG. style 'box': black bold text on a white rounded box (at the seam);
    'plain': large white text, no box (above the frame in the full-frame version).
    Shrinks the font until it fits in at most 3 lines. Returns (width, height)."""
    from PIL import Image, ImageDraw
    text = clean(text)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    plain = style == "plain"
    max_text_w = width * (0.9 if plain else 0.84)
    for size in ((72, 66, 60, 54, 48, 42) if plain else (60, 56, 52, 48, 44, 40, 36)):
        font, efont = _font("text", size), _font("emoji", size)
        lines = _wrap(probe, text, font, efont, size, max_text_w)
        if len(lines) <= 3:
            break
    line_h = int(size * 1.22)
    pad_x, pad_y = int(size * 0.45), int(size * 0.25)
    widths = [_measure(probe, ln, font, efont, size) for ln in lines]
    box_w = int(max(widths) + 2 * pad_x)
    box_h = int(line_h * len(lines) + 2 * pad_y)
    img = Image.new("RGBA", (width, box_h + 8), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    x0 = (width - box_w) // 2
    if not plain:
        draw.rounded_rectangle([x0, 4, x0 + box_w, 4 + box_h], radius=int(size * 0.35), fill=(255, 255, 255, 245))
    for i, (line, lw) in enumerate(zip(lines, widths)):
        x, y = (width - lw) / 2, 4 + pad_y + i * line_h
        for run, is_emoji in _runs(line):
            if is_emoji and efont is not None:
                for ch in [c for c in run if c not in "\uFE0F\u200D"]:
                    glyph = Image.new("RGBA", (int(size * 1.6), int(size * 1.6)), (0, 0, 0, 0))
                    ImageDraw.Draw(glyph).text((0, 0), ch, font=efont, embedded_color=True)
                    bbox = glyph.getbbox()
                    if bbox:
                        glyph = glyph.crop(bbox)
                        scale = size * 1.05 / max(glyph.size)
                        glyph = glyph.resize((max(1, int(glyph.width * scale)), max(1, int(glyph.height * scale))))
                        img.alpha_composite(glyph, (int(x), int(y + (line_h - glyph.height) / 2 - size * 0.08)))
                    x += size * 1.15
            else:
                if plain:  # white with a thin dark edge, so it reads on any background
                    draw.text((x, y), run, font=font, fill=(255, 255, 255, 255), stroke_width=2,
                              stroke_fill=(0, 0, 0, 200))
                else:
                    draw.text((x, y), run, font=font, fill=(0, 0, 0, 255))
                x += draw.textlength(run, font=font)
    img.save(out)
    return img.size


def seam_y(layout: str) -> int:
    """Where the headline sits: the line between cam and game (split), or the top of the video (blur)."""
    return {"split": 768, "blur": 656}.get(layout, 640)


def apply(base: str | Path, out: str | Path, text: str, seam: int, ffmpeg: str = "ffmpeg",
          style: str = "box") -> None:
    """Burn the headline into base -> out. style 'box': centred on the seam; 'plain': white text ending just
    above row `seam` (the top of the frame in the full-frame version). Empty text: out is a copy of base."""
    base, out = Path(base), Path(out)
    tmp = out.with_suffix(".tmp.mp4")
    if not text.strip():
        shutil.copyfile(base, tmp)
        tmp.replace(out)
        return
    png = out.with_suffix(".headline.png")
    _, h = render_png(text, png, style=style)
    y = max(0, min(OUT_H - h, seam - 36 - h if style == "plain" else seam - h // 2))
    p = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(base), "-i", str(png),
                        "-filter_complex", f"[0:v][1:v]overlay=0:{y}:format=auto,format=yuv420p[v]",
                        "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                        "-c:a", "copy", "-movflags", "+faststart", str(tmp)], capture_output=True, text=True)
    png.unlink(missing_ok=True)
    if p.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"headline overlay failed: {p.stderr[-300:]}")
    tmp.replace(out)
