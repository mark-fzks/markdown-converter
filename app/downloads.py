"""Short-lived download slots for files produced over MCP.

MCP tools return text, so a converted PDF cannot travel in the tool result. The
tool stores the file here instead and hands back a signed URL that stays valid
for a few minutes. Slots live in a temp directory and are swept on every access,
so nothing accumulates if the link is never used.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Download:
    token: str
    path: Path
    filename: str
    media_type: str
    expires_at: float

    @property
    def size(self) -> int:
        return self.path.stat().st_size if self.path.is_file() else 0


class DownloadStore:
    """An in-memory registry of files awaiting collection.

    Single-process by design: the converter runs as one container, and a link
    that outlives a restart would point at a file the restart already deleted.
    """

    def __init__(self, ttl_seconds: int = 900, max_entries: int = 200) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: dict[str, Download] = {}
        self._lock = threading.Lock()
        self._root = Path(tempfile.mkdtemp(prefix="mdconv-downloads-"))

    # -- lifecycle ---------------------------------------------------------

    def add(self, source: Path, filename: str, media_type: str) -> Download:
        """Move ``source`` into the store and return its slot."""
        self._sweep()

        token = secrets.token_urlsafe(32)
        target = self._root / token
        target.mkdir(parents=True, exist_ok=True)
        stored = target / filename
        shutil.copy2(source, stored)

        entry = Download(
            token=token,
            path=stored,
            filename=filename,
            media_type=media_type,
            expires_at=time.time() + self._ttl,
        )
        with self._lock:
            self._entries[token] = entry
        return entry

    def get(self, token: str) -> Download | None:
        """Look up a slot in constant time with respect to the token value."""
        self._sweep()
        with self._lock:
            for candidate, entry in self._entries.items():
                if hmac.compare_digest(candidate, token):
                    return entry if entry.path.is_file() else None
        return None

    def _sweep(self) -> None:
        now = time.time()
        with self._lock:
            expired = [t for t, e in self._entries.items() if e.expires_at <= now]
            # Also trim the oldest if a burst of conversions overshoots the cap.
            if len(self._entries) - len(expired) > self._max_entries:
                surviving = sorted(
                    (e for t, e in self._entries.items() if t not in expired),
                    key=lambda e: e.expires_at,
                )
                overflow = len(surviving) - self._max_entries
                expired += [e.token for e in surviving[:overflow]]
            for token in expired:
                self._entries.pop(token, None)

        for token in expired:
            shutil.rmtree(self._root / token, ignore_errors=True)

    def shutdown(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)
        with self._lock:
            self._entries.clear()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl
