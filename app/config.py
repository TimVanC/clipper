"""Settings: secrets come from the environment, tunables can also be edited live in the GUI."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import captions

LAYOUTS = ("auto", "split", "blur", "crop")
CATEGORY_PLATFORMS = ("twitch", "kick")
HEADLINE_MODES = ("auto", "claude", "local", "off")
POSTER_PROVIDERS = ("postforme", "uploadpost", "postproxy", "none")


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def parse_poster_providers(value: str) -> list[str]:
    """'postproxy, UploadPost' -> ['postproxy', 'uploadpost']; empty -> ['postforme']. Raises ValueError."""
    names = list(dict.fromkeys(p.strip().lower() for p in str(value).split(",") if p.strip())) or ["postforme"]
    bad = [n for n in names if n not in POSTER_PROVIDERS]
    if bad:
        raise ValueError(f"POSTER_PROVIDER must be one of {', '.join(POSTER_PROVIDERS)}, "
                         f"or several separated by commas (got {value!r})")
    if "none" in names and len(names) > 1:
        raise ValueError("POSTER_PROVIDER=none (manual mode) cannot be combined with other providers")
    return names


def parse_categories(value: str) -> list[tuple[str | None, str]]:
    """'Grand Theft Auto VI, kick:Fortnite' -> [(None, 'Grand Theft Auto VI'), ('kick', 'Fortnite')].
    A prefix is only a platform when it is one ('Call of Duty: Warzone' stays a name)."""
    out: list[tuple[str | None, str]] = []
    for part in str(value).replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        plat, sep, name = part.partition(":")
        if sep and plat.strip().lower() in CATEGORY_PLATFORMS:
            if not name.strip():
                raise ValueError(f"watch_categories: no category name after {plat.strip()}:")
            out.append((plat.strip().lower(), name.strip()))
        else:
            out.append((None, part))
    return out


def parse_start(value: str) -> float | None:
    """'2026-11-18 06:00' (this computer's time zone) or ISO with an offset -> epoch seconds; '' -> None."""
    from datetime import datetime
    if not str(value or "").strip():
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("T", " ").replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"category_start: expected a date and time like 2026-11-18 06:00, got {value!r}")
    return (dt if dt.tzinfo else dt.astimezone()).timestamp()


def split_list(value: str) -> list[str]:
    return [p.strip().lstrip("@").lower() for p in str(value).replace("\n", ",").split(",") if p.strip()]


def _read_dotenv(path: Path) -> dict[str, str]:
    """KEY=value lines from a .env file in the working directory (Docker passes them via env_file instead)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


# name -> (type, min, max) for GUI-editable tunables
TUNABLES: dict[str, tuple[type, float | None, float | None]] = {
    "poll_seconds": (int, 10, 3600),
    "offline_grace_polls": (int, 1, 20),
    "chunk_seconds": (int, 10, 3600),
    "pre_roll_s": (float, 0, 120),
    "post_roll_s": (float, 0, 120),
    "max_clip_s": (float, 10, 180),
    "min_clip_s": (float, 5, 120),
    "z_threshold": (float, 0.5, 20),
    "min_separation_s": (float, 5, 600),
    "max_clips_per_hour": (int, 1, 120),
    "warmup_s": (float, 0, 900),
    "audio_weight": (float, 0, 10),
    "chat_weight": (float, 0, 10),
    "chat_delay_s": (float, 0, 60),
    "max_posts_per_day": (int, 1, 500),
    "layout": (str, None, None),
    "facecam_boxes": (str, None, None),
    "watch_categories": (str, None, None),
    "category_streams": (int, 0, 10),
    "category_min_viewers": (int, 0, 10_000_000),
    "category_languages": (str, None, None),
    "category_exclude": (str, None, None),
    "category_start": (str, None, None),
    "headline_mode": (str, None, None),
    "auto_approve": (bool, None, None),
    "auto_post_min_rating": (float, 0, 10),
    "auto_post_version": (str, None, None),
    "full_frame_version": (bool, None, None),
    "delete_raw": (bool, None, None),
    "default_accounts": (str, None, None),
    "caption_template": (str, None, None),
    "caption_hooks": (str, None, None),
    "hashtags": (str, None, None),
    "max_hashtags": (int, 1, 15),
}


@dataclass
class Settings:
    data_dir: Path = Path("./data")

    # secrets / endpoints (env only)
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    kick_client_id: str = ""
    kick_client_secret: str = ""
    youtube_api_key: str = ""
    discord_webhook_url: str = ""
    postforme_api_key: str = ""
    postforme_base_url: str = "https://api.postforme.dev/v1"
    # postforme | uploadpost | postproxy | none (manual download); several can be combined: "postproxy,uploadpost"
    poster_provider: str = "postforme"
    uploadpost_api_key: str = ""
    uploadpost_base_url: str = "https://api.upload-post.com"
    postproxy_api_key: str = ""
    postproxy_base_url: str = "https://api.postproxy.dev"
    public_base_url: str = "http://localhost:8000"
    gui_user: str = "admin"
    gui_password: str = ""
    streamlink_cmd: str = "streamlink"
    ffmpeg_cmd: str = "ffmpeg"
    discord_max_upload_mb: float = 8.0
    anthropic_api_key: str = ""     # Claude writes the on-screen headline when set
    whisper_model: str = "small.en"  # speech-to-text model for the headline (tiny.en/base.en: faster, rougher)

    # tunables
    poll_seconds: int = 120
    offline_grace_polls: int = 2
    chunk_seconds: int = 600
    pre_roll_s: float = 40.0
    post_roll_s: float = 15.0
    max_clip_s: float = 90.0
    min_clip_s: float = 61.0  # clips are never shorter than this
    z_threshold: float = 3.0
    min_separation_s: float = 60.0
    max_clips_per_hour: int = 12
    warmup_s: float = 60.0
    audio_weight: float = 1.0
    chat_weight: float = 1.5
    chat_delay_s: float = 8.0
    max_posts_per_day: int = 20
    # auto: facecam on top + game below while a game is played and a cam is found, blur otherwise
    layout: str = "auto"
    facecam_boxes: str = ""         # "handle=x,y,w,h; ..." pins a streamer's cam (fractions of the frame)
    # record the biggest streams in these categories, whoever streams them: "Grand Theft Auto VI",
    # or per platform: "twitch:Grand Theft Auto VI, kick:Grand Theft Auto VI"
    watch_categories: str = ""
    category_streams: int = 3       # recorded at once from categories, all platforms together
    category_min_viewers: int = 1000
    category_languages: str = "en"  # comma separated; empty = any language
    category_exclude: str = ""      # handles never to pick from a category
    category_start: str = ""        # don't auto-watch before this time ("2026-11-18 06:00"), e.g. a game's launch
    # on-screen headline at the cam/game seam: auto = Claude when ANTHROPIC_API_KEY is set, else none;
    # claude | local (free: the most emphatic line said, can't tell the streamer from game dialogue) | off
    headline_mode: str = "auto"
    auto_approve: bool = False
    auto_post_min_rating: float = 0.0  # > 0: post clips rated at least this (0-10) without review
    auto_post_version: str = "headline"  # which version auto-posting uses: headline | plain | full | all
    full_frame_version: bool = True      # also render the full-frame version of each clip (one more render)
    delete_raw: bool = True
    default_accounts: str = ""
    # {hook} {hashtags} {name} {streamer} {game} {title} {platform}; see app/captions.py
    caption_template: str = captions.DEFAULT_TEMPLATE
    caption_hooks: str = ""             # one opening line per line; empty = the built-in rotation
    hashtags: str = "#clips #streamer"  # added after the streamer / game / platform tags
    max_hashtags: int = 5               # not counting the posting network's own tag (#fyp, #shorts)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = {**_read_dotenv(Path(".env")), **os.environ} if env is None else env  # real env vars win over .env
        provider = ",".join(parse_poster_providers(e.get("POSTER_PROVIDER", "")))
        s = cls(
            data_dir=Path(e.get("DATA_DIR", "./data")),
            twitch_client_id=e.get("TWITCH_CLIENT_ID", ""),
            twitch_client_secret=e.get("TWITCH_CLIENT_SECRET", ""),
            kick_client_id=e.get("KICK_CLIENT_ID", ""),
            kick_client_secret=e.get("KICK_CLIENT_SECRET", ""),
            youtube_api_key=e.get("YOUTUBE_API_KEY", ""),
            discord_webhook_url=e.get("DISCORD_WEBHOOK_URL", ""),
            postforme_api_key=e.get("POSTFORME_API_KEY", ""),
            postforme_base_url=e.get("POSTFORME_BASE_URL", "https://api.postforme.dev/v1").rstrip("/"),
            poster_provider=provider,
            uploadpost_api_key=e.get("UPLOADPOST_API_KEY", ""),
            uploadpost_base_url=e.get("UPLOADPOST_BASE_URL", "https://api.upload-post.com").rstrip("/"),
            postproxy_api_key=e.get("POSTPROXY_API_KEY", ""),
            postproxy_base_url=e.get("POSTPROXY_BASE_URL", "https://api.postproxy.dev").rstrip("/"),
            public_base_url=e.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
            gui_user=e.get("GUI_USER", "admin"),
            gui_password=e.get("GUI_PASSWORD", ""),
            streamlink_cmd=e.get("STREAMLINK_CMD", "streamlink"),
            ffmpeg_cmd=e.get("FFMPEG_CMD", "ffmpeg"),
            discord_max_upload_mb=float(e.get("DISCORD_MAX_UPLOAD_MB") or 8.0),
            anthropic_api_key=e.get("ANTHROPIC_API_KEY", ""),
            whisper_model=e.get("WHISPER_MODEL", "small.en") or "small.en",
        )
        overrides = {k: e[k.upper()] for k in TUNABLES if k.upper() in e}
        s.update(overrides)
        return s

    # ---- tunables -------------------------------------------------
    def tunables(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in TUNABLES}

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate + apply tunables. Raises ValueError on bad input, applies nothing in that case."""
        clean: dict[str, Any] = {}
        for k, v in values.items():
            if k not in TUNABLES:
                raise ValueError(f"unknown setting: {k}")
            typ, lo, hi = TUNABLES[k]
            try:
                val: Any = _bool(v) if typ is bool else typ(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k}: invalid value {v!r}")
            if typ in (int, float):
                if lo is not None and val < lo:
                    raise ValueError(f"{k}: must be >= {lo}")
                if hi is not None and val > hi:
                    raise ValueError(f"{k}: must be <= {hi}")
            if k == "layout" and val not in LAYOUTS:
                raise ValueError(f"layout must be one of {', '.join(LAYOUTS)}")
            if k == "facecam_boxes":
                from .facecam import parse_boxes
                parse_boxes(val)
            if k == "watch_categories":
                parse_categories(val)
            if k == "category_start":
                parse_start(val)
            if k == "auto_post_version" and val not in ("headline", "plain", "full", "all"):
                raise ValueError("auto_post_version must be headline, plain, full or all")
            if k == "headline_mode" and val not in HEADLINE_MODES:
                raise ValueError(f"headline_mode must be one of {', '.join(HEADLINE_MODES)}")
            if k == "caption_template":
                captions.validate(val.replace("\\n", "\n"))
            if k == "caption_hooks":
                captions.validate(captions.DEFAULT_TEMPLATE, val)
            clean[k] = val
        for k, val in clean.items():
            setattr(self, k, val)
        return clean

    def poster_names(self) -> list[str]:
        return parse_poster_providers(self.poster_provider)

    def default_account_ids(self) -> list[str]:
        return [a.strip() for a in self.default_accounts.split(",") if a.strip()]

    def configured(self) -> dict[str, bool]:
        out = {
            "twitch": bool(self.twitch_client_id and self.twitch_client_secret),
            "kick": bool(self.kick_client_id and self.kick_client_secret),
            "youtube": bool(self.youtube_api_key),
            "discord": bool(self.discord_webhook_url),
        }
        for name in self.poster_names():
            if name != "none":  # manual mode needs no poster key
                out[name] = bool(getattr(self, f"{name}_api_key", ""))
        return out
