"""Pluggable posting providers. Pick one with POSTER_PROVIDER (see app/config.py)."""
from __future__ import annotations

import httpx

from .base import Poster, PosterError
from .manual import ManualPoster
from .multi import MultiPoster
from .postforme import PostForMe, PostForMeError
from .postproxy import Postproxy, PostproxyError
from .uploadpost import UploadPost, UploadPostError

PROVIDERS: dict[str, type[Poster]] = {
    "postforme": PostForMe, "uploadpost": UploadPost, "postproxy": Postproxy, "none": ManualPoster}

__all__ = ["Poster", "PosterError", "ManualPoster", "MultiPoster", "PostForMe", "PostForMeError", "Postproxy", "PostproxyError",
           "UploadPost", "UploadPostError", "PROVIDERS", "make_poster"]


def make_poster(settings, http: httpx.AsyncClient) -> Poster:
    """Build the poster selected by settings.poster_provider (several names = one MultiPoster)."""
    names = settings.poster_names()  # validates
    if names == ["none"]:
        return ManualPoster()
    posters = [PROVIDERS[n](getattr(settings, f"{n}_api_key"), http, getattr(settings, f"{n}_base_url"))
               for n in names]
    return posters[0] if len(posters) == 1 else MultiPoster(posters)
