"""Captions and hashtags.

build_caption() writes the caption a clip gets when it is rendered (shown and editable in the Review tab).
for_platform() adapts that one caption to each network at posting time: the right discovery tag
(#fyp on TikTok, #shorts on YouTube, #reels on Instagram), no tags that belong to another network,
a clean YouTube title of at most 100 characters, and each network's length limit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# {name} = display name, {game} = current category. Kept generic on purpose: the app does not know
# what happens in a clip, so a hook must be true for any hype moment.
DEFAULT_HOOKS = [
    "{name} was NOT ready for this \U0001F62D",
    "{name} can't be serious \U0001F480",
    "Nobody saw this coming from {name} \U0001F633",
    "{name} lost it on stream \U0001F525",
    "This {name} moment is unreal \U0001F62D",
    "{name}'s chat went crazy for this \U0001F480",
    "{name} really did this live \U0001F633",
    "Wait for {name}'s reaction \U0001F62D",
]
GAME_HOOKS = [  # only used while the stream is in a real game category
    "{name} on {game} is something else \U0001F62D",
    "{game} broke {name} \U0001F480",
]
GAME_NAMES = {  # what people call the game, for the {game} in opening lines
    "grand theft auto vi": "GTA 6", "grand theft auto v": "GTA 5", "call of duty: warzone": "Warzone",
    "counter-strike": "CS2", "league of legends": "League",
}
NOT_A_GAME = {"", "just chatting", "irl", "special events", "talk shows & podcasts", "music", "sports", "asmr"}
GAME_TAGS = {  # categories whose plain name makes a poor hashtag
    "grand theft auto v": "gta", "grand theft auto vi": "gta6", "call of duty: warzone": "warzone",
    "counter-strike": "cs2", "ea sports fc 26": "fc26", "league of legends": "leagueoflegends",
    "talk shows & podcasts": "podcast", "special events": "",
}
DEFAULT_TEMPLATE = "{hook}\n\n{hashtags}"
PLACEHOLDERS = ("streamer", "name", "game", "title", "platform", "hook", "hashtags")
HOOK_PLACEHOLDERS = ("streamer", "name", "game", "title", "platform")

# the discovery tag each network wants, and the tags that only make sense on that network
PLATFORM_TAG = {"tiktok": "fyp", "youtube": "shorts", "instagram": "reels", "facebook": "reels"}
PLATFORM_ONLY = {"tiktok": {"fyp", "foryou", "foryoupage", "tiktok"}, "youtube": {"shorts", "youtubeshorts"},
                 "instagram": {"reels", "instagramreels"}, "facebook": {"reels"}}
TEXT_LIMIT = {"tiktok": 2200, "instagram": 2200, "youtube": 5000, "twitter": 280, "x": 280, "threads": 500,
              "bluesky": 300}
YOUTUBE_TITLE_MAX = 100
HASHTAG = re.compile(r"(?<!\w)#(?=\w*[^\W\d_])(\w+)")  # '#word', but not '#1'


def clean_category(name: str) -> str:
    """'Grand Theft Auto VI (GTA)' (Kick) -> 'Grand Theft Auto VI' (Twitch's name for the same game)."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", (name or "").strip())


def same_category(a: str, b: str) -> bool:
    return clean_category(a).lower() == clean_category(b).lower() != ""


def is_game(category: str) -> bool:
    """True for a real game category, False for Just Chatting, IRL, music... and unknown ('')."""
    return clean_category(category).lower() not in NOT_A_GAME


def tag(text: str) -> str:
    """'Just Chatting' -> 'justchatting', 'caseoh_' -> 'caseoh' (hashtags break on anything but letters/digits)."""
    return re.sub(r"[\W_]+", "", str(text).lower())


def display_name(handle: str, name: str = "") -> str:
    """Name used in the sentence: the platform's display name if known ('CaseOh_' -> 'CaseOh'), else the handle."""
    n = (name or handle or "").strip().strip("_").strip()
    return n[:1].upper() + n[1:] if n.islower() else n


def game_tag(game: str) -> str:
    g = clean_category(game).lower()
    return GAME_TAGS[g] if g in GAME_TAGS else tag(g)


def parse_tags(text: str) -> list[str]:
    """'#clips, streamer #Funny' -> ['clips', 'streamer', 'funny']"""
    return [t for t in (tag(p) for p in re.split(r"[\s,]+", text or "")) if t]


def build_hashtags(handle: str, platform: str = "", game: str = "", extra: str = "", limit: int = 5) -> list[str]:
    """Most specific first: the streamer, the game, '<streamer>clips', where it was streamed, then the broad ones.
    The posting network's own tag (#fyp, #shorts...) is added later by for_platform()."""
    who = tag(handle)
    wanted = [who, game_tag(game), f"{who}clips" if who else "", tag(platform)] + parse_tags(extra)
    out: list[str] = []
    for t in wanted:
        if t and t not in out and not any(t in own for own in PLATFORM_ONLY.values()):
            out.append(t)
    return out[:max(1, int(limit))]


def _hooks(custom: str, game: str) -> list[str]:
    own = [h.strip() for h in (custom or "").replace("\\n", "\n").splitlines() if h.strip()]
    if own:
        return own
    return DEFAULT_HOOKS + (GAME_HOOKS if is_game(game) else [])


def validate(template: str, hooks: str = "") -> None:
    """Raises ValueError if the template or a hook uses an unknown {placeholder}."""
    try:
        template.format(**{k: "x" for k in PLACEHOLDERS})
    except Exception:
        raise ValueError("caption_template may only use " + ", ".join("{%s}" % p for p in PLACEHOLDERS))
    for h in _hooks(hooks, ""):
        try:
            h.format(**{k: "x" for k in HOOK_PLACEHOLDERS})
        except Exception:
            raise ValueError("caption_hooks may only use " + ", ".join("{%s}" % p for p in HOOK_PLACEHOLDERS))


def build_caption(template: str = DEFAULT_TEMPLATE, *, handle: str, platform: str = "", name: str = "",
                  title: str = "", game: str = "", seed: int = 0, hooks: str = "", extra_tags: str = "",
                  max_tags: int = 5) -> str:
    """seed (the clip id) rotates through the hooks so consecutive clips don't read the same."""
    who = display_name(handle, name)
    g = clean_category(game)
    base = {"streamer": handle, "name": who, "game": GAME_NAMES.get(g.lower(), g), "title": (title or "").strip(),
            "platform": platform}
    pool = _hooks(hooks, game)
    try:
        hook = pool[seed % len(pool)].format(**base)
    except Exception:
        hook = DEFAULT_HOOKS[seed % len(DEFAULT_HOOKS)].format(**base)
    tags = " ".join("#" + t for t in build_hashtags(handle, platform, game, extra_tags, max_tags))
    try:
        text = (template or DEFAULT_TEMPLATE).replace("\\n", "\n").format(**base, hook=hook, hashtags=tags)
    except Exception:
        text = DEFAULT_TEMPLATE.format(**base, hook=hook, hashtags=tags)
    return _tidy(text)


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.strip().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@dataclass
class Tailored:
    text: str                 # caption / description for this network
    title: str                # short title (YouTube: <= 100 characters, ends with #shorts)
    tags: list[str] = field(default_factory=list)  # hashtags in `text`, without the '#'


def for_platform(caption: str, platform: str | None) -> Tailored:
    """Adapt one caption to a network. An unknown network gets the caption unchanged."""
    plat = (platform or "").lower()
    text = _tidy(caption or "")
    own = PLATFORM_ONLY.get(plat, set())
    foreign = {t for p, ts in PLATFORM_ONLY.items() if p != plat for t in ts} - own
    if plat in PLATFORM_ONLY and foreign:
        text = _tidy(HASHTAG.sub(lambda m: "" if m.group(1).lower() in foreign else m.group(0), text))
    want = PLATFORM_TAG.get(plat)
    if want and want not in {t.lower() for t in HASHTAG.findall(text)}:
        last = text.splitlines()[-1] if text else ""
        text = f"{text} #{want}" if HASHTAG.search(last) else f"{text}\n\n#{want}".strip()
    limit = TEXT_LIMIT.get(plat)
    if limit and len(text) > limit:  # drop hashtags from the end first, then cut
        while len(text) > limit and re.search(r"\s#\w+$", text) and len(HASHTAG.findall(text)) > 1:
            text = re.sub(r"\s+#\w+$", "", text)
        if len(text) > limit:
            text = text[:limit - 1].rstrip() + "…"
    first = next((ln for ln in text.splitlines() if HASHTAG.sub("", ln).strip()), "")
    title = re.sub(r"\s+", " ", HASHTAG.sub("", first)).strip() or "Clip"
    if plat == "youtube":
        suffix = " #shorts"
        if len(title) + len(suffix) > YOUTUBE_TITLE_MAX:
            title = title[:YOUTUBE_TITLE_MAX - len(suffix) - 1].rstrip() + "…"
        title += suffix
    return Tailored(text, title, [t.lower() for t in HASHTAG.findall(text)])
