"""Central paths and data directory for upwork-cron.

On Windows the writable state lives in %LOCALAPPDATA%\\upwork-cron\\data so an
installed copy does not write into site-packages. On other platforms the
historical ./data directory next to the project root is kept.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    override = os.environ.get("UPWORK_CRON_DATA")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "upwork-cron" / "data"
    return ROOT / "data"


DATA = _default_data_dir()
DB = DATA / "state.db"
LOCK = DATA / "watch.lock"
PID = DATA / "watcher.pid"
LOG = DATA / "watch.log"
WATCH_LOG = LOG
LOOP_LOG = DATA / "loop.log"
STDERR_LOG = DATA / "stderr.log"
TOKEN_PATH = DATA / "token.json"
MODE_FILE = DATA / "mode"


def ensure_data_dir() -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    return DATA
