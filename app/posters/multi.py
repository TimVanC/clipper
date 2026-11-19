"""Several providers at once (POSTER_PROVIDER=postproxy,uploadpost).

Account ids are prefixed with the provider name, "<provider>/<id>", e.g. "postproxy/AnUZrZ" or
"uploadpost/default:youtube"; publish() sends each provider only its own accounts.

If some providers succeed and others fail, publish() still returns (with "partial_error" set) instead of
raising: the clip counts as posted, so a retry can't post it a second time where it already went out.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .base import Poster, PosterError

log = logging.getLogger("clipper.poster")


class MultiPoster(Poster):
    def __init__(self, posters: list[Poster]):
        self.posters = posters
        self.by_name = {p.name: p for p in posters}
        self.name = ",".join(p.name for p in posters)
        self.label = " + ".join(p.label for p in posters)
        self.env_key = ", ".join(p.env_key for p in posters)
        self.key, self.http, self.base = "", None, ""

    @property
    def enabled(self) -> bool:
        return any(p.enabled for p in self.posters)

    def info(self) -> dict:
        missing = [p.env_key for p in self.posters if not p.enabled]
        return {**super().info(), "enabled": not missing, "env_key": ", ".join(missing) or self.env_key}

    async def list_accounts(self) -> list[dict]:
        out, errors = [], []
        for p in self.posters:
            if not p.enabled:
                continue
            try:
                out += [{**a, "id": f"{p.name}/{a['id']}"} for a in await p.list_accounts()]
            except Exception as e:  # one provider being down shouldn't hide the other's accounts
                log.warning("listing %s accounts failed: %s", p.label, e)
                errors.append(f"{p.label}: {e}")
        if errors and not out:
            raise PosterError("; ".join(errors)[:300])
        return out

    def _owner(self, post_id: str) -> tuple[Poster | None, str]:
        name, _, rest = post_id.partition(":")  # publish() returns "<provider>:<id>"
        return self.by_name.get(name), rest

    def checks(self, post_id: str) -> bool:
        p, rest = self._owner(post_id)
        return bool(p and p.checks(rest))

    async def check(self, post_id: str) -> dict:
        p, rest = self._owner(post_id)
        return await p.check(rest) if p else await super().check(post_id)

    async def publish(self, path: Path, caption: str, account_ids: list[str], external_id: str | None = None) -> dict:
        groups: dict[str, list[str]] = {}
        for a in account_ids:
            name, sep, rest = a.partition("/")
            if not sep or name not in self.by_name or not rest:
                raise PosterError(f"bad account id {a!r} (expected one of {', '.join(self.by_name)} + '/<id>')")
            groups.setdefault(name, []).append(rest)
        if not groups:
            raise PosterError("no social accounts selected")
        ids, raw, errors = [], {}, []
        for name, accts in groups.items():
            p = self.by_name[name]
            try:
                if not p.enabled:
                    raise PosterError(f"not configured (set {p.env_key})")
                res = await p.publish(path, caption, accts, external_id)
                raw[name] = res
                ids.append(f"{name}:{res.get('post_id', '')}")
                if res.get("partial_error"):
                    errors.append(f"{p.label}: {res['partial_error']}")
            except Exception as e:
                log.warning("%s publish failed: %s", p.label, e)
                errors.append(f"{p.label}: {e}")
        if not ids:
            raise PosterError("; ".join(errors)[:300])
        out: dict = {"post_id": ",".join(ids), "raw": raw}
        if errors:
            out["partial_error"] = ("posted via " + ", ".join(self.by_name[n].label for n in raw)
                                    + "; FAILED " + "; ".join(errors))[:300]
        return out
