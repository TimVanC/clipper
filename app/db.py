"""Tiny SQLite layer. One connection guarded by a lock; fine for a single-process app."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers(
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, handle TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, UNIQUE(platform, handle));
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY, platform TEXT, handle TEXT, title TEXT, stream_id TEXT,
  started_at REAL, ended_at REAL, dir TEXT);
CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY, session_id INTEGER, platform TEXT, handle TEXT,
  start_s REAL, end_s REAL, peak_s REAL, score REAL, audio_z REAL, chat_z REAL,
  path TEXT, thumb TEXT, status TEXT NOT NULL DEFAULT 'rendering',
  caption TEXT NOT NULL DEFAULT '', accounts TEXT NOT NULL DEFAULT '',
  post_id TEXT, post_error TEXT, created_at REAL, decided_at REAL, posted_at REAL);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS uploads(
  id INTEGER PRIMARY KEY, clip_id INTEGER NOT NULL, version TEXT NOT NULL, account TEXT NOT NULL,
  state TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '', sent_at REAL NOT NULL, checked_at REAL);
CREATE INDEX IF NOT EXISTS idx_uploads_clip ON uploads(clip_id);
CREATE INDEX IF NOT EXISTS idx_uploads_account ON uploads(account, sent_at);
"""

# columns added after the first release: headline = on-screen text burnt in at `seam` (pixel row);
# the text-free render is kept next to the clip as <id>_base.mp4 so the headline can be changed;
# plan = the uploads an approved clip should get, "version>account,..."
ADDED_COLUMNS = {"headline": "TEXT NOT NULL DEFAULT ''", "seam": "INTEGER NOT NULL DEFAULT 0",
                 "transcript": "TEXT NOT NULL DEFAULT ''", "plan": "TEXT NOT NULL DEFAULT ''"}

# queued: approved, waiting for a free posting slot or for its uploads to be confirmed
# expired: never posted or approved in time, its files were deleted to save disk space
CLIP_STATUSES = ("rendering", "pending", "queued", "posting", "posted", "approved", "failed", "rejected",
                 "expired")

# one upload = one version of a clip to one account.
# sending: being uploaded | processing: accepted, the network hasn't answered yet | live | failed |
# limited: turned down by a daily limit (TikTok / YouTube / Upload-Post), tried again later
UPLOAD_STATES = ("sending", "processing", "live", "failed", "limited")
COUNTED_STATES = ("sending", "processing", "live")  # what uses up an account's daily allowance


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            have = {r["name"] for r in self._conn.execute("PRAGMA table_info(clips)")}
            for col, decl in ADDED_COLUMNS.items():  # databases made by older versions
                if col not in have:
                    self._conn.execute(f"ALTER TABLE clips ADD COLUMN {col} {decl}")
            self._conn.commit()

    # ---- helpers -------------------------------------------------
    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            self._conn.commit()
            return cur

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(args)).fetchall()]

    def _one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # ---- streamers ------------------------------------------------
    def add_streamer(self, platform: str, handle: str) -> dict:
        platform, handle = platform.strip().lower(), handle.strip().lstrip("@")
        if platform not in ("twitch", "kick", "youtube"):
            raise ValueError("platform must be twitch, kick or youtube")
        if not handle:
            raise ValueError("handle required")
        if platform != "youtube":
            handle = handle.lower()
        self._exec("INSERT OR IGNORE INTO streamers(platform, handle) VALUES(?,?)", (platform, handle))
        return self._one("SELECT * FROM streamers WHERE platform=? AND handle=?", (platform, handle))  # type: ignore

    def list_streamers(self, enabled_only: bool = False) -> list[dict]:
        q = "SELECT * FROM streamers" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY platform, handle"
        return self._all(q)

    def set_streamer_enabled(self, streamer_id: int, enabled: bool) -> None:
        self._exec("UPDATE streamers SET enabled=? WHERE id=?", (int(enabled), streamer_id))

    def delete_streamer(self, streamer_id: int) -> None:
        self._exec("DELETE FROM streamers WHERE id=?", (streamer_id,))

    # ---- sessions -------------------------------------------------
    def start_session(self, platform: str, handle: str, title: str, stream_id: str, dir_: str) -> int:
        cur = self._exec(
            "INSERT INTO sessions(platform, handle, title, stream_id, started_at, dir) VALUES(?,?,?,?,?,?)",
            (platform, handle, title, stream_id, time.time(), dir_),
        )
        return int(cur.lastrowid)

    def end_session(self, session_id: int) -> None:
        self._exec("UPDATE sessions SET ended_at=? WHERE id=?", (time.time(), session_id))

    def end_open_sessions(self) -> list[int]:
        """At startup nothing is recording: close the sessions a stopped app left open."""
        ids = [r["id"] for r in self._all("SELECT id FROM sessions WHERE ended_at IS NULL")]
        if ids:
            self._exec(f"UPDATE sessions SET ended_at=? WHERE id IN ({','.join('?' * len(ids))})", (time.time(), *ids))
        return ids

    def clips_with_files(self, statuses: tuple[str, ...]) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        return self._all(f"SELECT * FROM clips WHERE status IN ({marks}) AND path IS NOT NULL AND path!='' "
                         "ORDER BY MAX(COALESCE(created_at,0), COALESCE(decided_at,0))", statuses)

    def list_sessions(self, limit: int = 20) -> list[dict]:
        return self._all("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))

    # ---- clips ----------------------------------------------------
    def add_clip(self, session_id: int, platform: str, handle: str, start_s: float, end_s: float,
                 peak_s: float, score: float, audio_z: float, chat_z: float) -> int:
        cur = self._exec(
            """INSERT INTO clips(session_id, platform, handle, start_s, end_s, peak_s, score, audio_z, chat_z,
                                 status, created_at) VALUES(?,?,?,?,?,?,?,?,?,'rendering',?)""",
            (session_id, platform, handle, start_s, end_s, peak_s, score, audio_z, chat_z, time.time()),
        )
        return int(cur.lastrowid)

    def get_clip(self, clip_id: int) -> dict | None:
        return self._one("SELECT * FROM clips WHERE id=?", (clip_id,))

    def list_clips(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status:
            return self._all("SELECT * FROM clips WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        return self._all("SELECT * FROM clips ORDER BY id DESC LIMIT ?", (limit,))

    def update_clip(self, clip_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"path", "thumb", "status", "caption", "accounts", "post_id", "post_error",
                   "decided_at", "posted_at", "headline", "seam", "transcript", "plan"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {bad}")
        if "status" in fields and fields["status"] not in CLIP_STATUSES:
            raise ValueError("bad status")
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE clips SET {sets} WHERE id=?", (*fields.values(), clip_id))

    def claim_clip(self, clip_id: int, from_statuses: tuple[str, ...], to_status: str) -> bool:
        """Atomic status transition; returns False if the clip wasn't in one of from_statuses."""
        marks = ",".join("?" * len(from_statuses))
        cur = self._exec(
            f"UPDATE clips SET status=?, decided_at=? WHERE id=? AND status IN ({marks})",
            (to_status, time.time(), clip_id, *from_statuses),
        )
        return cur.rowcount == 1

    def count_posted_since(self, since_ts: float) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM clips WHERE status IN ('posted','posting') AND decided_at>=?",
                        (since_ts,))
        return int(row["n"]) if row else 0

    # ---- uploads -------------------------------------------------
    def add_upload(self, clip_id: int, version: str, account: str, state: str = "sending",
                   sent_at: float | None = None, **fields: Any) -> int:
        cur = self._exec("INSERT INTO uploads(clip_id, version, account, state, sent_at) VALUES(?,?,?,?,?)",
                         (clip_id, version, account, state, time.time() if sent_at is None else sent_at))
        uid = int(cur.lastrowid)
        if fields:
            self.update_upload(uid, **fields)
        return uid

    def update_upload(self, upload_id: int, **fields: Any) -> None:
        bad = set(fields) - {"state", "request_id", "url", "error", "checked_at"}
        if bad:
            raise ValueError(f"cannot update {bad}")
        if "state" in fields and fields["state"] not in UPLOAD_STATES:
            raise ValueError("bad upload state")
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE uploads SET {sets} WHERE id=?", (*fields.values(), upload_id))

    def uploads_for(self, clip_id: int) -> list[dict]:
        return self._all("SELECT * FROM uploads WHERE clip_id=? ORDER BY id", (clip_id,))

    def uploads_for_clips(self, clip_ids: list[int]) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {i: [] for i in clip_ids}
        if clip_ids:
            for u in self._all(f"SELECT * FROM uploads WHERE clip_id IN ({','.join('?' * len(clip_ids))}) "
                               "ORDER BY id", clip_ids):
                out[u["clip_id"]].append(u)
        return out

    def uploads_in(self, *states: str) -> list[dict]:
        return self._all(f"SELECT * FROM uploads WHERE state IN ({','.join('?' * len(states))}) ORDER BY id", states)

    def account_uploads_since(self, account: str, since_ts: float) -> list[dict]:
        return self._all("SELECT * FROM uploads WHERE account=? AND sent_at>=? ORDER BY sent_at", (account, since_ts))

    def clear_failed_uploads(self, clip_id: int) -> None:
        """Before a retry: the failed attempts make way for new ones."""
        self._exec("DELETE FROM uploads WHERE clip_id=? AND state IN ('failed','limited')", (clip_id,))

    def fail_interrupted_uploads(self) -> list[int]:
        """Uploads the app was in the middle of sending when it stopped: whether they arrived is unknown."""
        rows = self.uploads_in("sending")
        for u in rows:
            self.update_upload(u["id"], state="failed", error="the app was stopped while uploading: check the "
                                                              "profile before retrying, it may be up already")
        return [u["clip_id"] for u in rows]

    def fail_interrupted_posts(self) -> list[int]:
        """Clips left in 'posting' by a stopped app. They may have reached some networks, so they become
        'failed' with a note rather than being retried (a retry could post twice)."""
        ids = [r["id"] for r in self._all("SELECT id FROM clips WHERE status='posting'")]
        if ids:
            self._exec(f"UPDATE clips SET status='failed', post_error=? WHERE id IN ({','.join('?' * len(ids))})",
                       ("the app was stopped while posting: check your profiles before retrying, "
                        "it may already be up on some of them", *ids))
        return ids

    def fail_interrupted_renders(self) -> list[int]:
        """Clips left in 'rendering' by a stopped app: nothing will ever finish them."""
        ids = [r["id"] for r in self._all("SELECT id FROM clips WHERE status='rendering'")]
        if ids:
            self._exec(f"UPDATE clips SET status='failed', post_error=? WHERE id IN ({','.join('?' * len(ids))})",
                       ("render was cut off when the app stopped", *ids))
        return ids

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in CLIP_STATUSES}
        for r in self._all("SELECT status, COUNT(*) AS n FROM clips GROUP BY status"):
            out[r["status"]] = r["n"]
        return out

    # ---- settings -------------------------------------------------
    def get_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._all("SELECT key, value FROM settings")}

    def put_settings(self, values: dict[str, Any]) -> None:
        with self._lock:
            for k, v in values.items():
                self._conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
            self._conn.commit()
