"""Self-update for installed copies and source checkouts.

Installed copies keep their token, database, logs and keywords in the data
directory next to (not inside) `app/`, so replacing the app code is safe. The
watcher is stopped for the swap and restarted afterwards if it was running.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import dispatch, runner, schedule
from .notify import log

REPO = "Logisite/upwork-mcp-cron"
DEFAULT_REF = "main"

_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "data", "__pycache__",
    ".pytest_cache", "dist", "build", "*.egg-info",
)


def install_layout() -> tuple[Path, Path, Path] | None:
    """(install root, app dir, venv python) for an installed copy, else None."""
    # Do NOT resolve(): a venv's `python` is a symlink to the system interpreter,
    # and resolving it would point at /usr/bin instead of the install root.
    exe = Path(sys.executable).absolute()
    if exe.parent.name not in ("bin", "Scripts"):
        return None
    root = exe.parent.parent.parent   # .../upwork-cron/venv/bin/python
    app = root / "app"
    venv = root / "venv"
    if venv.is_dir() and (app / "upwork_cron").is_dir() and (app / "pyproject.toml").is_file():
        return root, app, exe
    return None


def download_source(ref: str, tmp: Path) -> Path:
    """Fetch a codeload zip and return the directory containing pyproject.toml."""
    url = f"https://codeload.github.com/{REPO}/zip/{ref}"
    with urllib.request.urlopen(url, timeout=90) as response:
        payload = response.read()
    zpath = tmp / "source.zip"
    zpath.write_bytes(payload)
    out = tmp / "src"
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.extractall(out)
    for child in sorted(out.iterdir()):
        if (child / "pyproject.toml").is_file() and (child / "upwork_cron").is_dir():
            return child
    raise RuntimeError("download did not contain a Python project")


def fingerprint(root: Path) -> str:
    """Hash app source so updates work even without a version bump."""
    files = sorted((root / "upwork_cron").glob("*.py"))
    files.append(root / "pyproject.toml")
    h = hashlib.sha256()
    for path in files:
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()


def version(root: Path) -> str:
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    m = re.search(r'^version\s*=\s*["\']([^"\']+)', text, re.MULTILINE)
    return m.group(1) if m else "unknown"


def update(ref: str = DEFAULT_REF, check_only: bool = False) -> int:
    layout = install_layout()
    if layout:
        root, app, vpy = layout
        target: Path = app
    else:
        root = Path(__file__).resolve().parent.parent
        if not (root / ".git").is_dir():
            print("update failed: no installed app directory and no git checkout", file=sys.stderr)
            return 1
        vpy = Path(sys.executable)
        target = root

    print(f"Checking {REPO} ({ref}) ...")
    tmp = Path(tempfile.mkdtemp(prefix="upcron-update-"))
    backup: Path | None = None
    stopped = False
    try:
        src = download_source(ref, tmp)
        print(f"installed : {version(target)}")
        print(f"available : {version(src)}")
        if check_only:
            if fingerprint(target) == fingerprint(src):
                print("Up to date.")
            else:
                print("Update available: run `upcron update`.")
            return 0

        active = bool(runner.running_pid() or schedule.installed())
        if layout and active:
            print("Stopping watcher and schedule ...")
            runner.stop()
            stopped = True

        if layout:
            new_app = tmp / "app"
            shutil.copytree(src, new_app, ignore=_IGNORE)
            generated = app / "run.sh"
            if generated.is_file():
                shutil.copy2(generated, new_app / "run.sh")
            backup = root / "app-update-backup"
            shutil.rmtree(backup, ignore_errors=True)
            app.rename(backup)
            shutil.move(str(new_app), str(app))
            print("Installing dependencies ...")
            subprocess.run(
                [str(vpy), "-m", "pip", "install", "--upgrade", "-e", str(app)],
                cwd=str(root), check=True,
            )
            shutil.rmtree(backup, ignore_errors=True)
            backup = None
        else:
            print("Pulling latest source ...")
            subprocess.run(["git", "-C", str(root), "pull", "--ff-only"], check=True)
            subprocess.run(
                [str(vpy), "-m", "pip", "install", "-e", str(root)],
                cwd=str(root), check=True,
            )

        if layout and stopped:
            print("Restarting watcher and schedule ...")
            runner.start(termux=dispatch.termux_mode())
            stopped = False

        print(f"Update complete: now {version(target)}")
        log(f"updated to {version(target)} ({ref})")
        return 0
    except urllib.error.URLError as e:
        print(f"update failed: could not download source: {e}", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"update failed: {e.cmd} exited {e.returncode}", file=sys.stderr)
    except Exception as e:
        print(f"update failed: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        if backup is not None and backup.exists():
            current = root / "app"
            if current.exists():
                shutil.rmtree(current, ignore_errors=True)
            backup.rename(current)
        if stopped:
            print("Restoring previous watcher state ...", file=sys.stderr)
            try:
                runner.start(termux=dispatch.termux_mode())
            except Exception as e:
                print(f"could not restart watcher; run `upcron start`: {e}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
    return 1
