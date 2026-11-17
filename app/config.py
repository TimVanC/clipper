"""Settings: secrets come from the environment, tunables can also be edited live in the GUI."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

LAYOUTS = ("blur", "crop")
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
    "auto_approve": (bool, None, None),
    "delete_raw": (bool, None, None),
    "default_accounts": (str, None, None),
    "caption_template": (str, None, None),
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
    layout: str = "blur"
    auto_approve: bool = False
    delete_raw: bool = True
    default_accounts: str = ""
    caption_template: str = "{streamer} went crazy \U0001F525 #{streamer} #clips #fyp"

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
                raise ValueError(f"layout must be one of {LAYOUTS}")
            if k == "caption_template":
                try:
                    val.format(streamer="x")
                except Exception:
                    raise ValueError("caption_template may only use {streamer}")
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
