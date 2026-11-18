# Clipper: auto-clip streamers and post the best moments

Watches the streamers you pick, records them while live, finds the hype moments, cuts vertical clips
with 40 s of build-up, pings you on Discord, and posts to your socials when you tap **Approve**.

```
 Twitch / Kick / YouTube APIs ──poll every 2 min──▶ Watcher ──live──▶ Discord "X is live"
                                                       │
                                streamlink ─▶ ffmpeg ──▶ 10-min chunks (Recorder)
                                                       │
        audio loudness  +  Twitch chat rate/emotes ──▶ HypeDetector (robust z-scores, peak picking)
                                                       │  window = onset - 40 s … peak + 15 s
                                          ffmpeg render 1080x1920 (blurred-background layout)
                                                       │
                          Discord "clip ready" + preview ─▶ GUI review page ─▶ [Approve]
                                                       │
          Poster (POSTER_PROVIDER): Post for Me | Upload-Post | Postproxy → TikTok / Reels / Shorts / X …
                                    or none → clip marked approved, [Download] in the GUI
```

## Run it

```bash
cp .env.example .env        # fill in what you have (see below)
pip install -r requirements.txt   # needs ffmpeg installed too
python -m app.doctor        # checks every credential with one real call each
python -m app.main          # GUI on http://localhost:8000
```
Docker: `docker compose up -d` (volume `/data` holds recordings, clips and the database).
Needs a box that stays on and has bandwidth/disk: recording is about 2-4 GB/hour per live 1080p stream
(raw chunks are deleted as they age out; `delete_raw` in Settings).

Then in the GUI: **Live** tab → add streamers (`twitch` handle, `kick` slug, or YouTube `@handle`/`UC…` id) →
**Settings** → put your provider's account ids in *Default accounts* (the Review tab lists them as checkboxes too).
The Settings tab shows which posting provider is active.

## Posting providers

Posting is pluggable (`app/posters/`, one class per provider implementing `list_accounts` and `publish`).
Pick one with `POSTER_PROVIDER` in `.env` and restart:

| `POSTER_PROVIDER` | Key | Account ids look like | Notes |
|---|---|---|---|
| `postforme` (default) | `POSTFORME_API_KEY` | `spc_…` | upload URL → PUT file → create post |
| `uploadpost` | `UPLOADPOST_API_KEY` | `profile:platform`, e.g. `main:tiktok` | one multipart upload per profile, sent async |
| `postproxy` | `POSTPROXY_API_KEY` | profile id, e.g. `prof_abc123` | one multipart post; provider defaults for TikTok privacy / YouTube title |
| `none` | none | n/a | **free manual mode** |

**Several providers at once**: `POSTER_PROVIDER=postproxy,uploadpost` (any mix except `none`). Each needs its
key. Account ids are then prefixed with the provider, e.g. `postproxy/prof_abc123,uploadpost/main:youtube`, and
one Approve sends the clip to each provider's accounts. If one provider fails and another succeeds, the clip is
marked `posted` with the failure shown on it, so a retry can't double-post; repost the failed part by hand.

**Manual mode (`none`)**: Approve only marks the clip `approved`; nothing is uploaded and no poster key or
account is needed. The Review and History tabs show a **Download** button so you can post the file yourself.
The daily post cap does not apply. `python -m app.doctor` checks only the selected provider.
Account ids are provider-specific, so update *Default accounts* when you switch.

## Captions and hashtags

Each clip gets a caption when it is rendered (`app/captions.py`); you can still edit it in the Review tab.

- **Opening line.** A rotation of short hooks ("CaseOh was NOT ready for this"), picked by clip id so two clips in
  a row never read the same. In a real game category, game hooks join the rotation. Put your own in
  *Settings → Opening lines* (one per line, `{name}` and `{game}` allowed).
- **Hashtags, specific to broad.** `#<streamer>`, `#<game>`, `#<streamer>clips`, `#twitch` / `#kick`, then your
  *Extra hashtags*, capped at *Max hashtags* (default 5). Handles are cleaned first (`caseoh_` → `#caseoh`).
- **Per network, at posting time.** TikTok gets `#fyp`; YouTube gets `#shorts`, a title of at most 100 characters
  (the opening line + `#shorts`), the full caption as description, and the hashtags as video tags; Instagram gets
  `#reels`. A tag that belongs to another network is removed, and X / Threads / Bluesky length limits are respected.
- **Template.** *Settings → Caption template*, default `{hook}` + blank line + `{hashtags}`. Also available:
  `{name}`, `{streamer}`, `{game}`, `{title}` (stream title), `{platform}`.

The app does not know what happens in a clip, so the opening lines are generic by design.

## Credentials

| Variable | Where | Needed for |
|---|---|---|
| `POSTER_PROVIDER` | `postforme`, `uploadpost`, `postproxy` or `none` | which poster is used |
| `POSTFORME_API_KEY` | postforme.dev (connect your social accounts there) | posting via Post for Me |
| `UPLOADPOST_API_KEY` | upload-post.com | posting via Upload-Post |
| `POSTPROXY_API_KEY` | postproxy.dev | posting via Postproxy |
| `DISCORD_WEBHOOK_URL` | Discord channel → Integrations → Webhooks | notifications |
| `TWITCH_CLIENT_ID/SECRET` | dev.twitch.tv/console | detecting Twitch streams |
| `KICK_CLIENT_ID/SECRET` | kick.com/settings/developer | detecting Kick streams |
| `YOUTUBE_API_KEY` | Google Cloud → YouTube Data API v3 | detecting YouTube streams |
| `GUI_PASSWORD`, `PUBLIC_BASE_URL` | you choose | protecting the GUI; links inside Discord |

Only the platforms you track need keys. Twitch chat is read anonymously (no login needed).

## How "hype" is detected

Per second: loudness of the stream audio (dB) and, on Twitch, chat messages weighted up for hype
emotes/emoji/ALL CAPS. Each is converted to a robust z-score against the previous 30 minutes of that
stream (median/MAD, so a quiet streamer's shout and a loud streamer's shout are both "big").
`score = (w_audio·z_audio + w_chat·z_chat) / √(w_audio² + w_chat²)`. Local maxima above `z_threshold`
(default 3.0), at least `min_separation_s` apart, become clips: `pre_roll_s` (40 s) before the onset of the
rise to `post_roll_s` (15 s) after the peak, capped at `max_clip_s`. Every clip is at least `min_clip_s`
(61 s by default) long: shorter windows get extra build-up added before the moment. Chat is shifted back by `chat_delay_s`
because chat reacts a few seconds late. Too many clips → raise `z_threshold`; missing moments → lower it.

## Tests (the pyramid)

```bash
pip install -r requirements-dev.txt
make unit          # 50 tests, ~1 s: detection maths, parsers, state machines, API clients (mocked HTTP)
make integration   # real ffmpeg / real streamlink over local HLS / real socket / real ASGI app
make e2e           # synthetic stream → clip → Discord → approve → Post for Me, incl. chat timing
```

## Known limits (read these)

- **Chat signal is Twitch-only.** Kick and YouTube streams are scored on audio alone.
- **"Laughter" is approximated by loudness**, not a trained laughter model. Loud ≠ funny; the review step is
  there for a reason. Whisper-based transcript scoring is the natural next upgrade.
- **Poster endpoint paths** (`app/posters/*.py`) follow each provider's docs but were verified against mocks, not
  live accounts. `python -m app.doctor` tests the selected one with your key; if a path differs, it's one constant.
- **Upload-Post and Postproxy report success asynchronously.** A clip is marked `posted` once the provider accepts
  the upload; a later per-platform failure on their side is not polled back.
- **TikTok via Postproxy posts with comments, duet and stitch disabled.** That is Postproxy's default and the
  switch (`platforms.tiktok.disable_comment`) is only documented for JSON bodies, not for file uploads.
- **Clipping rights.** Only clip streamers who allow it, or run paid clipping campaigns that authorise you.
  Posting someone else's footage without permission can get accounts struck.
- YouTube live detection relies on live broadcasts appearing in the channel's uploads playlist
  (about 2 quota units per check instead of 100 for search).
