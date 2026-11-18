"""Upload-Post client (docs.upload-post.com).

Verified against the docs (2026-11):
- auth header `Authorization: Apikey <key>`, base URL https://api.upload-post.com
- GET  /api/uploadposts/users -> {"profiles": [{"username", "social_accounts": {platform: {...} | "" | null}}]}
- POST /api/upload (multipart): `user`, repeated `platform[]`, `video` (file), `title`, `description`,
  `async_upload`, `external_id`, and per-platform captions: `<platform>_title` (falls back to `title`),
  `youtube_description` (falls back to `description`), `tags[]` (YouTube tags). TikTok's caption is its
  title field; `description` is only used by YouTube, LinkedIn, Facebook and Pinterest. Sync answer: {"success", "results": {platform: {"success", "error"}}};
  async answer: {"success", "request_id"}.

Upload-Post has no per-account id: an upload targets a profile (`user`) plus platform names. Account ids
here are therefore "<profile>:<platform>", and publish() sends one upload per profile.
"""
from __future__ import annotations

from pathlib import Path

from ..captions import for_platform
from .base import Poster, PosterError

PATH_USERS = "/api/uploadposts/users"
PATH_UPLOAD = "/api/upload"
# platforms with a documented `<platform>_title` override
TITLE_OVERRIDES = {"tiktok", "instagram", "youtube", "linkedin", "facebook", "x", "pinterest", "bluesky", "threads",
                   "reddit"}
YOUTUBE_TAGS_MAX = 15


class UploadPostError(PosterError):
    pass


class UploadPost(Poster):
    name, label, env_key = "uploadpost", "Upload-Post", "UPLOADPOST_API_KEY"

    def __init__(self, api_key, http, base_url: str = "https://api.upload-post.com"):
        super().__init__(api_key, http, base_url)

    def _h(self) -> dict:
        return {"Authorization": f"Apikey {self.key}"}

    @staticmethod
    def _check(r, what: str) -> dict:
        try:
            j = r.json() if r.content else {}
        except ValueError:
            j = {}
        if r.status_code >= 400 or (isinstance(j, dict) and j.get("success") is False):
            msg = (j.get("message") or j.get("error")) if isinstance(j, dict) else None
            raise UploadPostError(f"{what} -> {r.status_code}: {msg or r.text[:300]}")
        return j if isinstance(j, dict) else {}

    async def list_accounts(self) -> list[dict]:
        r = await self.http.get(self.base + PATH_USERS, headers=self._h())
        out = []
        for p in self._check(r, f"GET {PATH_USERS}").get("profiles", []):
            for platform, acct in (p.get("social_accounts") or {}).items():
                if not isinstance(acct, dict) or not acct:  # "" / null = not connected
                    continue
                who = acct.get("handle") or acct.get("display_name") or acct.get("username") or ""
                out.append({"id": f"{p['username']}:{platform}", "platform": platform,
                            "username": f"{who} ({p['username']})" if who else p["username"],
                            "status": "reauth_required" if acct.get("reauth_required") else "connected"})
        return out

    async def publish(self, path: Path, caption: str, account_ids: list[str], external_id: str | None = None) -> dict:
        by_profile: dict[str, list[str]] = {}
        for a in account_ids:
            profile, _, platform = a.rpartition(":")
            if not profile or not platform:
                raise UploadPostError(f"bad account id {a!r} (expected profile:platform)")
            by_profile.setdefault(profile, []).append(platform)
        if not by_profile:
            raise UploadPostError("no social accounts selected")
        video = Path(path).read_bytes()
        ids, raw = [], []
        for profile, platforms in by_profile.items():
            data: dict = {"user": profile, "platform[]": platforms, "title": caption, "description": caption,
                          "async_upload": "true"}
            for platform in platforms:  # each network gets its own wording, see app/captions.py
                t = for_platform(caption, platform)
                if platform == "youtube":
                    data["youtube_title"], data["youtube_description"] = t.title, t.text
                    data["tags[]"] = [x for x in t.tags if x != "shorts"][:YOUTUBE_TAGS_MAX]
                elif platform in TITLE_OVERRIDES:
                    data[f"{platform}_title"] = t.text
            if external_id:
                data["external_id"] = external_id
            r = await self.http.post(self.base + PATH_UPLOAD, headers=self._h(), data=data,
                                     files={"video": (Path(path).name, video, "video/mp4")}, timeout=600)
            j = self._check(r, f"POST {PATH_UPLOAD} ({profile})")
            results = j.get("results")
            if isinstance(results, dict) and results:  # sync answer: surface per-platform failures
                bad = {k: v.get("error") or v.get("skip_reason") or "failed" for k, v in results.items()
                       if isinstance(v, dict) and (v.get("success") is False or v.get("skipped"))}
                if bad:
                    raise UploadPostError("; ".join(f"{k}: {v}" for k, v in bad.items())[:300])
            ids.append(str(j.get("request_id") or j.get("job_id") or ""))
            raw.append(j)
        return {"post_id": ",".join(i for i in ids if i), "raw": raw}
