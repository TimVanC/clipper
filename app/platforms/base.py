from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LiveInfo:
    platform: str
    handle: str
    title: str = ""
    stream_id: str = ""
    url: str = ""
    viewers: int = 0


class PlatformClient(Protocol):
    async def check_many(self, handles: list[str]) -> dict[str, LiveInfo | None]:
        """Map lowercase handle -> LiveInfo (live) / None (offline).
        A handle that is missing from the result means 'unknown' (e.g. per-handle API error)."""
        ...
