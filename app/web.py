"""FastAPI GUI + JSON API. The GUI is a single static page (mobile friendly) in app/static/index.html."""
from __future__ import annotations

import hmac
import re
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .pipeline import ClipError, Pipeline
from .rating import clip_rating, rating_label

STATIC = Path(__file__).parent / "static"
security = HTTPBasic(auto_error=False)


class StreamerIn(BaseModel):
    platform: str
    handle: str


class ApproveIn(BaseModel):
    caption: str | None = None
    accounts: list[str] | None = None


def create_app(pl: Pipeline, start_pipeline: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_pipeline:
            await pl.start()
        yield
        if start_pipeline:
            await pl.stop()

    app = FastAPI(title="Clipper", docs_url=None, redoc_url=None, lifespan=lifespan)
    s = pl.s

    def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        if not s.gui_password:  # no password set = open (fine on localhost, set one when deployed)
            return
        ok = creds is not None and hmac.compare_digest(creds.username.encode(), s.gui_user.encode()) \
            and hmac.compare_digest(creds.password.encode(), s.gui_password.encode())
        if not ok:
            raise HTTPException(401, "auth required", headers={"WWW-Authenticate": 'Basic realm="clipper"'})

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    async def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    # ---- state ----
    @app.get("/api/state", dependencies=[Depends(auth)])
    async def state() -> dict:
        return {**pl.state(), "settings": s.tunables()}

    @app.post("/api/poll", dependencies=[Depends(auth)])
    async def poll() -> dict:
        await pl.poll_now()
        return pl.state()

    # ---- streamers ----
    @app.post("/api/streamers", dependencies=[Depends(auth)])
    async def add_streamer(body: StreamerIn) -> dict:
        try:
            return pl.db.add_streamer(body.platform, body.handle)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.patch("/api/streamers/{sid}", dependencies=[Depends(auth)])
    async def toggle_streamer(sid: int, enabled: bool) -> dict:
        pl.db.set_streamer_enabled(sid, enabled)
        return {"ok": True}

    @app.delete("/api/streamers/{sid}", dependencies=[Depends(auth)])
    async def del_streamer(sid: int) -> dict:
        pl.db.delete_streamer(sid)
        return {"ok": True}

    # ---- clips ----
    @app.get("/api/clips", dependencies=[Depends(auth)])
    async def clips(status: str | None = None, limit: int = 100) -> list[dict]:
        rows = pl.db.list_clips(status, min(limit, 500))
        return [_public(r) for r in rows]

    @app.post("/api/clips/{cid}/approve", dependencies=[Depends(auth)])
    async def approve(cid: int, body: ApproveIn) -> dict:
        try:
            return _public(await pl.approve(cid, body.caption, body.accounts))
        except ClipError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/clips/{cid}/reject", dependencies=[Depends(auth)])
    async def reject(cid: int) -> dict:
        try:
            return _public(pl.reject(cid))
        except ClipError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/clips/{cid}/video", dependencies=[Depends(auth)])
    async def video(cid: int, download: bool = False):
        c = pl.db.get_clip(cid)
        if not c or not c.get("path") or not Path(c["path"]).exists():
            raise HTTPException(404, "no video")
        if download:  # Content-Disposition: attachment, for manual posting
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", c.get("handle") or "clip")
            return FileResponse(c["path"], media_type="video/mp4", filename=f"{safe}-clip-{cid}.mp4")
        return FileResponse(c["path"], media_type="video/mp4")

    @app.get("/api/clips/{cid}/thumb", dependencies=[Depends(auth)])
    async def thumb(cid: int):
        c = pl.db.get_clip(cid)
        if not c or not c.get("thumb") or not Path(c["thumb"]).exists():
            raise HTTPException(404, "no thumb")
        return FileResponse(c["thumb"], media_type="image/jpeg")

    # ---- settings / integrations ----
    @app.put("/api/settings", dependencies=[Depends(auth)])
    async def put_settings(values: dict) -> dict:
        try:
            applied = s.update(values)
        except ValueError as e:
            raise HTTPException(400, str(e))
        pl.db.put_settings(applied)
        return s.tunables()

    @app.get("/api/accounts", dependencies=[Depends(auth)])
    async def accounts() -> list[dict]:
        if not pl.poster.enabled:
            return []
        try:
            return await pl.poster.list_accounts()
        except Exception as e:
            raise HTTPException(502, f"{pl.poster.label}: {e}")

    @app.post("/api/test/discord", dependencies=[Depends(auth)])
    async def test_discord() -> dict:
        return {"ok": await pl.notifier.test()}

    return app


def _public(c: dict) -> dict:
    r = clip_rating(c.get("score"), c.get("audio_z"), c.get("chat_z"))
    return {k: v for k, v in c.items() if k not in ("path", "thumb")} | {
        "has_video": bool(c.get("path")), "has_thumb": bool(c.get("thumb")),
        "rating": r, "rating_label": rating_label(r)}
