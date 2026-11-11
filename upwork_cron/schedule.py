"""Background schedule ownership.

Linux: user crontab line calling run.sh/ensure.
Windows: per-user Task Scheduler task running `pythonw -m upwork_cron ensure`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .notify import log
from .paths import ROOT

TASK_NAME = "UpworkCron"
CRON_LINE = f"* * * * * {ROOT / 'run.sh'}"


def _pythonw() -> str:
    exe = Path(sys.executable)
    if sys.platform == "win32":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


# --- Linux cron ---

def _read_crontab() -> list[str]:
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10
        )
        if out.returncode != 0:
            return []
        return out.stdout.splitlines()
    except Exception as e:
        log(f"crontab read failed: {e}")
        return []


def _cron_installed() -> bool:
    marker = str(ROOT / "run.sh")
    return any(marker in line for line in _read_crontab())


def _install_cron() -> bool:
    marker = str(ROOT / "run.sh")
    lines = _read_crontab()
    if any(marker in line for line in lines):
        return True
    lines.append(CRON_LINE)
    try:
        proc = subprocess.run(
            ["crontab", "-"], input="\n".join(lines) + "\n",
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            log("crontab entry installed")
            return True
        log(f"crontab install failed: {proc.stderr.strip()}")
    except Exception as e:
        log(f"crontab install failed: {e}")
    return False


def _remove_cron() -> bool:
    marker = str(ROOT / "run.sh")
    lines = _read_crontab()
    kept = [ln for ln in lines if marker not in ln]
    if len(kept) == len(lines):
        return True
    try:
        if kept:
            proc = subprocess.run(
                ["crontab", "-"], input="\n".join(kept) + "\n",
                capture_output=True, text=True, timeout=10,
            )
        else:
            proc = subprocess.run(
                ["crontab", "-r"], capture_output=True, text=True, timeout=10
            )
        if proc.returncode == 0:
            log("crontab entry removed")
            return True
        log(f"crontab remove failed: {proc.stderr.strip()}")
    except Exception as e:
        log(f"crontab remove failed: {e}")
    return False


# --- Windows Task Scheduler ---

def _task_installed() -> bool:
    try:
        out = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True, text=True, timeout=15,
        )
        return out.returncode == 0
    except Exception:
        return False


def _install_task() -> bool:
    if _task_installed():
        try:
            subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, timeout=15)
        except Exception:
            pass
    tr = f'"{_pythonw()}" -m upwork_cron ensure'
    cmd = [
        "schtasks", "/Create", "/F",
        "/SC", "MINUTE", "/MO", "1",
        "/TN", TASK_NAME,
        "/TR", tr,
        "/IT",
        "/RL", "LIMITED",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            log(f"schtasks create failed: {proc.stderr.strip() or proc.stdout.strip()}")
            return False
        # Make it resilient: start when available, don't stop on battery, allow overlap ignore.
        ps = (
            f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}'; "
            "$s = $t.Settings; "
            "$s.StartWhenAvailable = $true; "
            "$s.DisallowStartIfOnBatteries = $false; "
            "$s.StopIfGoingOnBatteries = $false; "
            "$s.ExecutionTimeLimit = 'PT0S'; "
            "$s.MultipleInstances = 'IgnoreNew'; "
            f"Set-ScheduledTask -TaskName '{TASK_NAME}' -Settings $s | Out-Null"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
        log("scheduled task installed")
        return True
    except Exception as e:
        log(f"schtasks create failed: {e}")
        return False


def _remove_task() -> bool:
    if not _task_installed():
        return True
    try:
        proc = subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            log("scheduled task removed")
            return True
        log(f"schtasks delete failed: {proc.stderr.strip() or proc.stdout.strip()}")
    except Exception as e:
        log(f"schtasks delete failed: {e}")
    return False


def installed() -> bool:
    if sys.platform == "win32":
        return _task_installed()
    return _cron_installed()


def install() -> bool:
    if sys.platform == "win32":
        return _install_task()
    return _install_cron()


def remove() -> bool:
    if sys.platform == "win32":
        return _remove_task()
    return _remove_cron()


def describe() -> str:
    if sys.platform == "win32":
        return f"Task Scheduler \\{TASK_NAME} (every 1 minute)"
    return CRON_LINE


def query() -> dict:
    """Best-effort schedule details for status/doctor."""
    info = {"installed": installed(), "platform": sys.platform}
    if sys.platform == "win32":
        info["name"] = TASK_NAME
        try:
            out = subprocess.run(
                ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
                capture_output=True, text=True, timeout=15,
            )
            info["query_ok"] = out.returncode == 0
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    k = k.strip().lower()
                    v = v.strip()
                    if k == "status":
                        info["status"] = v
                    elif k == "last run time":
                        info["last_run"] = v
                    elif k == "last result":
                        info["last_result"] = v
                    elif k == "next run time":
                        info["next_run"] = v
                    elif k == "task to run":
                        info["task_to_run"] = v
        except Exception as e:
            info["error"] = str(e)
    else:
        info["cron"] = _cron_installed()
        info["line"] = CRON_LINE
    return info
