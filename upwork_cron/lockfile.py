"""Cross-platform non-blocking exclusive file lock."""

from __future__ import annotations

import os
import sys
from typing import IO, Optional

from .paths import LOCK, ensure_data_dir


def acquire() -> Optional[IO[str]]:
    """Acquire the watcher lock, or return None if another run holds it."""
    ensure_data_dir()
    fh = open(LOCK, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            fh.write("0")
            fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (BlockingIOError, OSError):
        fh.close()
        return None


def release(fh: Optional[IO[str]]) -> None:
    if fh is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        fh.close()
