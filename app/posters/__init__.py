"""Pluggable posting providers. Pick one with POSTER_PROVIDER (see app/config.py)."""
from __future__ import annotations

import httpx

from .base import Poster, PosterError
from .manual import ManualPoster
from .postforme import PostForMe, PostForMeError
from .postproxy import Postproxy, PostproxyError
from .uploadpost import UploadPost, UploadPostError

PROVIDERS: dict[str, type[Poster]] = {
    "postforme": PostForMe, "uploadpost": UploadPost, "postproxy": Postproxy, "none": ManualPoster}

__all__ = ["Poster", "PosterError", "ManualPoster", "PostForMe", "PostForMeError", "Postproxy", "PostproxyError",
           "UploadPost", "UploadPostError", "PROVIDERS", "make_poster"]


def make_poster(settings, http: httpx.AsyncClient) -> Poster:
    """Build the poster selected by settings.poster_provider."""
    name = settings.poster_provider
    if name not in PROVIDERS:
        raise ValueError(f"POSTER_PROVIDER must be one of {', '.join(PROVIDERS)} (got {name!r})")
    if name == "none":
        return ManualPoster()
    return PROVIDERS[name](getattr(settings, f"{name}_api_key"), http, getattr(settings, f"{name}_base_url"))
