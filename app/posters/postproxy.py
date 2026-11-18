"""Postproxy client (postproxy.dev/reference).

Verified against the docs (2026-11):
- auth header `Authorization: Bearer <key>`, base URL https://api.postproxy.dev, no version segment
- GET  /api/profiles -> {"data": [{"id", "name", "username", "platform", "status"}]}
- POST /api/posts (multipart): `post[body]`, repeated `profiles[]` (profile ids), `media[]` (file).
  Answer: {"id", "status", "platforms": [{"platform", "profile_id", "status", "error"}]}.

Not sent: per-platform `platforms` params (TikTok privacy, YouTube title...). The docs only show them
for JSON bodies, not for multipart uploads, so the provider defaults apply. Per the TikTok page those
defaults are: public, comments / duet / stitch DISABLED (`disable_comment` etc. default to true).

A post has one `body` for all its profiles, so publish() sends one post per network, each with the
caption adapted to that network (app/captions.py). The network of a profile id comes from /api/profiles.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..captions import for_platform
from .base import Poster, PosterError

log = logging.getLogger("clipper.poster")

PATH_PROFILES = "/api/profiles"
PATH_POSTS = "/api/posts"


class PostproxyError(PosterError):
    pass


class Postproxy(Poster):
    name, label, env_key = "postproxy", "Postproxy", "POSTPROXY_API_KEY"

    def __init__(self, api_key, http, base_url: str = "https://api.postproxy.dev"):
        super().__init__(api_key, http, base_url)
        self._platform_of: dict[str, str] = {}  # profile id -> network

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    @staticmethod
    def _check(r, what: str) -> dict:
        if r.status_code >= 400:
            try:
                j = r.json()
                msg = j.get("message") or j.get("error") or "; ".join(map(str, j.get("errors") or []))
            except (ValueError, AttributeError):
                msg = ""
            raise PostproxyError(f"{what} -> {r.status_code}: {msg or r.text[:300]}")
        return r.json() if r.content else {}

    async def list_accounts(self) -> list[dict]:
        r = await self.http.get(self.base + PATH_PROFILES, headers=self._h())
        return [{"id": p["id"], "platform": p.get("platform", ""), "username": p.get("username") or p.get("name") or "",
                 "status": p.get("status", "")} for p in self._check(r, f"GET {PATH_PROFILES}").get("data", [])]

    async def get_post(self, post_id: str) -> dict:
        r = await self.http.get(f"{self.base}{PATH_POSTS}/{post_id}", headers=self._h())
        return self._check(r, f"GET {PATH_POSTS}/{post_id}")

    async def _platforms(self, account_ids: list[str]) -> dict[str, str]:
        if any(a not in self._platform_of for a in account_ids):
            try:
                self._platform_of = {a["id"]: a["platform"] for a in await self.list_accounts()}
            except Exception as e:  # posting still works, just with the caption as written
                log.warning("could not look up Postproxy profiles, captions are not adapted: %s", e)
        return self._platform_of

    async def publish(self, path: Path, caption: str, account_ids: list[str], external_id: str | None = None) -> dict:
        if not account_ids:
            raise PostproxyError("no social accounts selected")
        platform_of = await self._platforms(account_ids)
        groups: dict[str, list[str]] = {}
        for a in account_ids:
            groups.setdefault(platform_of.get(a, ""), []).append(a)
        video = Path(path).read_bytes()
        ids, raw, errors = [], [], []
        for platform, accts in groups.items():
            try:
                r = await self.http.post(self.base + PATH_POSTS, headers=self._h(),
                                         data={"post[body]": for_platform(caption, platform).text, "profiles[]": accts},
                                         files={"media[]": (Path(path).name, video, "video/mp4")}, timeout=600)
                j = self._check(r, f"POST {PATH_POSTS}")
                bad = [f"{p.get('platform')}: {p.get('error') or 'failed'}" for p in j.get("platforms") or []
                       if p.get("status") == "failed"]
                if bad:
                    raise PostproxyError("; ".join(bad)[:300])
                ids.append(str(j.get("id", "")))
                raw.append(j)
            except PostproxyError as e:
                errors.append(e)
        if not ids:
            raise errors[0]
        out: dict = {"post_id": ",".join(ids), "raw": raw[0] if len(raw) == 1 else raw}
        if errors:  # some networks went out: report, don't raise, so a retry can't double-post
            out["partial_error"] = "; ".join(map(str, errors))[:300]
        return out
