"""Postproxy client (postproxy.dev/reference).

Verified against the docs (2026-11):
- auth header `Authorization: Bearer <key>`, base URL https://api.postproxy.dev, no version segment
- GET  /api/profiles -> {"data": [{"id", "name", "username", "platform", "status"}]}
- POST /api/posts (multipart): `post[body]`, repeated `profiles[]` (profile ids), `media[]` (file).
  Answer: {"id", "status", "platforms": [{"platform", "profile_id", "status", "error"}]}.

Not sent: per-platform `platforms` params (TikTok privacy, YouTube title...). The docs only show them
for JSON bodies, not for multipart uploads, so the provider defaults apply.
"""
from __future__ import annotations

from pathlib import Path

from .base import Poster, PosterError

PATH_PROFILES = "/api/profiles"
PATH_POSTS = "/api/posts"


class PostproxyError(PosterError):
    pass


class Postproxy(Poster):
    name, label, env_key = "postproxy", "Postproxy", "POSTPROXY_API_KEY"

    def __init__(self, api_key, http, base_url: str = "https://api.postproxy.dev"):
        super().__init__(api_key, http, base_url)

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

    async def publish(self, path: Path, caption: str, account_ids: list[str], external_id: str | None = None) -> dict:
        if not account_ids:
            raise PostproxyError("no social accounts selected")
        r = await self.http.post(self.base + PATH_POSTS, headers=self._h(),
                                 data={"post[body]": caption, "profiles[]": list(account_ids)},
                                 files={"media[]": (Path(path).name, Path(path).read_bytes(), "video/mp4")},
                                 timeout=600)
        j = self._check(r, f"POST {PATH_POSTS}")
        bad = [f"{p.get('platform')}: {p.get('error') or 'failed'}" for p in j.get("platforms") or []
               if p.get("status") == "failed"]
        if bad:
            raise PostproxyError("; ".join(bad)[:300])
        return {"post_id": str(j.get("id", "")), "raw": j}
