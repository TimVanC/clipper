"""Post for Me client: upload the clip, then publish to the selected connected social accounts.

Flow (from Post for Me's SDK/docs): create an upload URL -> PUT the file bytes -> create a social post
whose `media[].url` is the returned public `media_url` and whose `social_accounts` are account ids.
Endpoint paths are constants so they're easy to adjust; run `python -m app.doctor` to verify them
against your account.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .base import Poster, PosterError

log = logging.getLogger("clipper.poster")

PATH_UPLOAD_URL = "/media/create-upload-url"
PATH_POSTS = "/social-posts"
PATH_ACCOUNTS = "/social-accounts"
PATH_RESULTS = "/social-post-results"


class PostForMeError(PosterError):
    pass


class PostForMe(Poster):
    name, label, env_key = "postforme", "Post for Me", "POSTFORME_API_KEY"

    def __init__(self, api_key, http, base_url: str = "https://api.postforme.dev/v1"):
        super().__init__(api_key, http, base_url)

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    async def _json(self, method: str, path: str, **kw) -> dict:
        r = await self.http.request(method, self.base + path, headers=self._h(), **kw)
        if r.status_code >= 400:
            raise PostForMeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def list_accounts(self) -> list[dict]:
        j = await self._json("GET", PATH_ACCOUNTS, params={"limit": 100})
        return [{"id": a["id"], "platform": a.get("platform", ""), "username": a.get("username") or a.get("name", ""),
                 "status": a.get("status", "")} for a in j.get("data", [])]

    async def upload_media(self, path: Path) -> str:
        j = await self._json("POST", PATH_UPLOAD_URL)
        upload_url, media_url = j.get("upload_url"), j.get("media_url")
        if not upload_url or not media_url:
            raise PostForMeError(f"unexpected upload-url response: {str(j)[:200]}")
        r = await self.http.put(upload_url, content=Path(path).read_bytes(), headers={"Content-Type": "video/mp4"},
                                timeout=600)
        if r.status_code >= 400:
            raise PostForMeError(f"media upload failed: {r.status_code} {r.text[:200]}")
        return media_url

    async def create_post(self, caption: str, account_ids: list[str], media_url: str,
                          external_id: str | None = None) -> dict:
        if not account_ids:
            raise PostForMeError("no social accounts selected")
        body: dict = {"caption": caption, "social_accounts": account_ids, "media": [{"url": media_url}]}
        if external_id:
            body["external_id"] = external_id
        return await self._json("POST", PATH_POSTS, json=body)

    async def results(self, post_id: str) -> list[dict]:
        j = await self._json("GET", PATH_RESULTS, params={"post_id": post_id})
        return j.get("data", [])

    async def publish(self, path: Path, caption: str, account_ids: list[str], external_id: str | None = None) -> dict:
        media_url = await self.upload_media(path)
        post = await self.create_post(caption, account_ids, media_url, external_id)
        return {"post_id": post.get("id", ""), "media_url": media_url, "raw": post}
