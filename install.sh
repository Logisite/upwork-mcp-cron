#!/usr/bin/env bash
# upwork-cron Linux installer (per-user, no root required).
#
# Installs into ~/.local/share/upwork-cron, creates ~/.local/bin/upcron,
# runs browser sign-in, then starts the watcher and its cron self-heal.
#
# Usage: ./install.sh [--skip-auth] [--skip-start] [--ref REF]

set -euo pipefail

APP_NAME="upwork-cron"
REPO="Logisite/upwork-mcp-cron"
REF="main"
SKIP_AUTH=0
SKIP_START=0
DOWNLOADED_SRC=""

usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  --skip-auth    Do not run the browser sign-in step.
  --skip-start   Do not start the watcher or install the cron entry.
  --ref REF      Git ref to download when run outside a source checkout
                 (default: main).
  -h, --help     Show this help.
EOF
}

say() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
  printf '\nERROR: %s\n' "$*" >&2
  printf 'Install did not complete.\n' >&2
  exit 1
}

cleanup() {
  if [ -n "${DOWNLOADED_SRC:-}" ]; then
    rm -rf "$DOWNLOADED_SRC"
  fi
}
on_cancel() {
  printf '\nInstall cancelled.\n' >&2
  printf 'Re-run the installer to finish.\n' >&2
  exit 130
}
trap cleanup EXIT
trap on_cancel INT TERM

while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-auth) SKIP_AUTH=1 ;;
    --skip-start) SKIP_START=1 ;;
    --ref)
      shift
      [ "$#" -gt 0 ] || die '--ref needs a value.'
      REF="$1"
      ;;
    --ref=*) REF="${1#*=}" ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) die "Unknown option: $1 (see --help)." ;;
  esac
  shift
done

[ "$(uname -s 2>/dev/null || true)" = "Linux" ] || die 'This installer is for Linux.'
if [ -n "${PREFIX:-}" ] && [[ "$PREFIX" == *com.termux* ]]; then
  die 'This installer is for desktop Linux. On Termux use the -t flow (see README).'
fi
if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
  die 'HOME is not set to a valid directory.'
fi
if [ "$(id -u)" -eq 0 ]; then
  warn "Running as root installs into root's home. Run this as your normal user for a per-user install."
fi

XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
INSTALL_ROOT="$XDG_DATA/$APP_NAME"
APP_DIR="$INSTALL_ROOT/app"
VENV_DIR="$INSTALL_ROOT/venv"
DATA_DIR="$INSTALL_ROOT/data"
BIN_DIR="$HOME/.local/bin"
PROFILE="$HOME/.profile"

say "Locating Python 3.10+"
PY=""
PYVER=""
for cand in python3 python; do
  command -v "$cand" >/dev/null 2>&1 || continue
  ver="$("$cand" -c 'import sys; print(str(sys.version_info[0]) + "." + str(sys.version_info[1]))' 2>/dev/null || true)"
  if [[ "$ver" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
    maj="${BASH_REMATCH[1]}"
    min="${BASH_REMATCH[2]}"
    if [ "$maj" -eq 3 ] && [ "$min" -ge 10 ]; then
      PY="$cand"
      PYVER="$ver"
      break
    fi
  fi
done
if [ -z "$PY" ]; then
  printf 'Python 3.10 or newer was not found.\n' >&2
  printf 'Tried: python3, python\n' >&2
  printf 'Install Python 3.10+ with your package manager, then run this installer again.\n' >&2
  exit 1
fi
printf 'Using Python %s via %s\n' "$PYVER" "$PY"

say "Preparing source"
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fi
SRC_DIR="$SCRIPT_DIR"
if [ -n "$SRC_DIR" ]; then
  case "$SRC_DIR/" in
    "$INSTALL_ROOT/"*) SRC_DIR="" ;;
  esac
fi
if [ -z "$SRC_DIR" ] || [ ! -f "$SRC_DIR/pyproject.toml" ] || [ ! -d "$SRC_DIR/upwork_cron" ]; then
  SRC_DIR=""
  say "Downloading upwork-cron source ($REF)"
  DOWNLOADED_SRC="$(mktemp -d "${TMPDIR:-/tmp}/upcron-src.XXXXXX")"
  zip="$DOWNLOADED_SRC/source.zip"
  url="https://codeload.github.com/$REPO/zip/$REF"
  if command -v curl >/dev/null 2>&1; then
    curl -fL "$url" -o "$zip" || die "Could not download source from $url"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$zip" "$url" || die "Could not download source from $url"
  else
    die 'curl or wget is required to download the source.'
  fi
  extract="$DOWNLOADED_SRC/src"
  "$PY" -m zipfile -e "$zip" "$extract" || die 'Could not extract downloaded source.'
  for d in "$extract"/*; do
    if [ -f "$d/pyproject.toml" ] && [ -d "$d/upwork_cron" ]; then
      SRC_DIR="$d"
      break
    fi
  done
  [ -n "$SRC_DIR" ] || die 'Downloaded source did not contain pyproject.toml.'
fi
printf 'Source: %s\n' "$SRC_DIR"

say "Creating install directories"
mkdir -p "$INSTALL_ROOT" "$DATA_DIR" "$BIN_DIR"
# Run all Python commands from the install root, not the source checkout.
# `python -m` puts the current directory first on sys.path, so a source
# checkout would otherwise shadow the installed editable package.
cd "$INSTALL_ROOT"

if [ -x "$VENV_DIR/bin/python" ]; then
  say "Stopping any running watcher"
  set +e
  UPWORK_CRON_DATA="$DATA_DIR" "$VENV_DIR/bin/python" -m upwork_cron stop
  rc=$?
  set -e
  if [ "$rc" -eq 130 ]; then on_cancel; fi
  if [ "$rc" -ne 0 ]; then
    warn 'Existing watcher did not stop cleanly; continuing.'
  fi
fi

say "Copying application source to $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
tar -C "$SRC_DIR" \
  --exclude=.git \
  --exclude=.venv \
  --exclude=venv \
  --exclude=data \
  --exclude=__pycache__ \
  --exclude=.pytest_cache \
  --exclude=dist \
  --exclude=build \
  --exclude='*.egg-info' \
  -cf - . | tar -C "$APP_DIR" -xf -

say "Creating virtual environment"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  if ! "$PY" -m venv "$VENV_DIR"; then
    die 'Could not create a virtual environment. On Debian/Ubuntu install python3-venv; on Fedora install python3; then run this installer again.'
  fi
fi
VPY="$VENV_DIR/bin/python"
"$VPY" -m pip install --upgrade pip setuptools wheel || die 'pip upgrade failed.'
"$VPY" -m pip install -e "$APP_DIR" || die 'Dependency installation failed.'

say "Creating the upcron command"
DATA_Q="$(printf '%q' "$DATA_DIR")"
ROOT_Q="$(printf '%q' "$INSTALL_ROOT")"
VPY_Q="$(printf '%q' "$VPY")"
write_shim() {
  local target="$1"
  cat > "$target" <<EOF
#!/usr/bin/env bash
# Generated by the upwork-cron Linux installer.
cd $ROOT_Q || exit 1
export UPWORK_CRON_DATA=$DATA_Q
exec $VPY_Q -m upwork_cron "\$@"
EOF
  chmod +x "$target"
}
write_shim "$BIN_DIR/upcron"
ln -sfn upcron "$BIN_DIR/upwork-cron"

RUN_SH="$APP_DIR/run.sh"
cat > "$RUN_SH" <<EOF
#!/usr/bin/env bash
# Generated by the upwork-cron Linux installer. Cron self-heal entrypoint.
cd $ROOT_Q || exit 1
export UPWORK_CRON_DATA=$DATA_Q
exec $VPY_Q -m upwork_cron ensure
EOF
chmod +x "$RUN_SH"

say "Checking your PATH"
case ":$PATH:" in
  *":$BIN_DIR:"*)
    printf '%s is already on your PATH.\n' "$BIN_DIR"
    ;;
  *)
    if [ -f "$PROFILE" ] && grep -qF '# >>> upwork-cron installer >>>' "$PROFILE"; then
      printf 'PATH entry already present in %s.\n' "$PROFILE"
    else
      {
        printf '\n# >>> upwork-cron installer >>>\n'
        printf 'case ":$PATH:" in *":%s:"*) ;; *) PATH="%s:$PATH"; export PATH ;; esac\n' "$BIN_DIR" "$BIN_DIR"
        printf '# <<< upwork-cron installer <<<\n'
      } >> "$PROFILE"
      printf 'Added %s to PATH in %s.\n' "$BIN_DIR" "$PROFILE"
    fi
    ;;
esac
export PATH="$BIN_DIR:$PATH"
export UPWORK_CRON_DATA="$DATA_DIR"

if [ "$SKIP_AUTH" -eq 0 ]; then
  say "Upwork sign-in"
  token="$DATA_DIR/token.json"
  if [ -f "$token" ]; then
    printf 'Existing token found at %s; skipping browser sign-in.\n' "$token"
  else
    printf 'A browser window will open so you can approve access. If it does not, copy the printed URL.\n'
    printf 'Press Ctrl+C to cancel.\n'
    set +e
    "$VPY" -m upwork_cron auth
    rc=$?
    set -e
    if [ "$rc" -eq 130 ]; then on_cancel; fi
    if [ "$rc" -ne 0 ]; then die 'Authentication did not complete.'; fi
  fi
else
  printf 'Skipping auth (--skip-auth). Run: upcron auth\n'
fi

if [ "$SKIP_START" -eq 0 ]; then
  say "Starting watcher and installing schedule"
  if ! command -v crontab >/dev/null 2>&1; then
    warn 'crontab was not found. The watcher will run now, but it will not restart automatically.'
    warn 'Install cron (Debian/Ubuntu: sudo apt install cron), then run: upcron stop && upcron start'
  fi
  set +e
  "$VPY" -m upwork_cron start
  rc=$?
  set -e
  if [ "$rc" -eq 130 ]; then on_cancel; fi
  if [ "$rc" -ne 0 ]; then die 'Failed to start the watcher.'; fi
  if command -v crontab >/dev/null 2>&1; then
    if ! "$VPY" -c 'from upwork_cron import schedule; raise SystemExit(0 if schedule.installed() else 1)' >/dev/null 2>&1; then
      warn 'The watcher is running, but the cron entry could not be installed. Run: upcron doctor'
    fi
  fi
else
  printf 'Skipping start (--skip-start). Run: upcron start\n'
fi

if command -v notify-send >/dev/null 2>&1; then
  say "Notification test"
  "$VPY" -m upwork_cron notify-test || warn 'Notification test command failed.'
else
  warn 'notify-send was not found. Desktop notifications will not work until libnotify is installed.'
fi

say "Install complete"
cat <<EOF
Install root : $INSTALL_ROOT
App code     : $APP_DIR
Data/logs    : $DATA_DIR
Command      : $BIN_DIR/upcron

upcron is ready. If PATH was just changed, open a new terminal, or run:
  $BIN_DIR/upcron status

  upcron status
  upcron doctor
  upcron logs -n 50
  upcron stop
  upcron start
EOF
