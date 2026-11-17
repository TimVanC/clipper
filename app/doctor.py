"""`python -m app.doctor` - checks every credential/integration with one cheap real call each."""
from __future__ import annotations

import asyncio
import shutil
import sys

import httpx

from .config import Settings
from .notifier import DiscordNotifier
from .platforms.kick import KickClient
from .platforms.twitch import TwitchClient
from .platforms.youtube import YouTubeClient
from .posters import make_poster


async def run(s: Settings) -> int:
    failures = 0

    def report(name: str, ok: bool | None, detail: str = "") -> None:
        nonlocal failures
        mark = "SKIP" if ok is None else ("OK  " if ok else "FAIL")
        if ok is False:
            failures += 1
        print(f"[{mark}] {name:<22} {detail}")

    for tool in ("ffmpeg", "ffprobe", s.streamlink_cmd):
        report(f"binary: {tool}", shutil.which(tool) is not None, shutil.which(tool) or "not found on PATH")

    async with httpx.AsyncClient(timeout=20) as http:
        if s.twitch_client_id and s.twitch_client_secret:
            try:
                r = await TwitchClient(s.twitch_client_id, s.twitch_client_secret, http).check_many(["twitch"])
                report("Twitch API", True, f"token ok, query ok ({len(r)} channel checked)")
            except Exception as e:
                report("Twitch API", False, str(e)[:150])
        else:
            report("Twitch API", None, "TWITCH_CLIENT_ID/SECRET not set")
        if s.kick_client_id and s.kick_client_secret:
            try:
                r = await KickClient(s.kick_client_id, s.kick_client_secret, http).check_many(["kick"])
                report("Kick API", True, "token ok, channels query ok")
            except Exception as e:
                report("Kick API", False, str(e)[:150])
        else:
            report("Kick API", None, "KICK_CLIENT_ID/SECRET not set")
        if s.youtube_api_key:
            try:
                await YouTubeClient(s.youtube_api_key, http).channel_id("YouTube")
                report("YouTube API", True, "key ok")
            except Exception as e:
                report("YouTube API", False, str(e)[:150])
        else:
            report("YouTube API", None, "YOUTUBE_API_KEY not set")
        if s.discord_webhook_url:
            ok = await DiscordNotifier(s.discord_webhook_url, http).test()
            report("Discord webhook", ok, "test message sent" if ok else "webhook rejected the message")
        else:
            report("Discord webhook", None, "DISCORD_WEBHOOK_URL not set")
        poster = make_poster(s, http)  # only the provider chosen by POSTER_PROVIDER is checked
        name = f"Poster: {poster.label}"
        if poster.manual:
            report(name, None, "POSTER_PROVIDER=none: nothing to check, clips are downloaded from the GUI")
        elif not poster.enabled:
            report(name, None, f"{poster.env_key} not set")
        else:
            try:
                accts = await poster.list_accounts()
                report(name, True, f"{len(accts)} connected account(s): "
                       + ", ".join(f"{a['platform']}:{a['username']}({a['id']})" for a in accts))
            except Exception as e:
                report(name, False, str(e)[:200])
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(Settings.from_env())))
