#!/usr/bin/env bash
# Install the dependencies and start the Horizon Discord/GitHub bot.
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
REQUIREMENTS="requirements.txt"
LOCK="$VENV/.requirements.lock"
REQUIRED_KEYS="DISCORD_TOKEN DISCORD_CHANNEL_ID GITHUB_TOKEN GITHUB_PROJECT_ID"
install=1

usage() {
  cat <<'USAGE'
Usage: ./run.sh [options]

Creates .venv if needed, installs requirements.txt and starts the bot.

Options:
  -n, --no-install   Skip the dependency step and start the bot right away.
  -h, --help         Show this message.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--no-install) install=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; echo >&2; usage >&2; exit 2 ;;
  esac
  shift
done

fail() { echo "Error: $*" >&2; exit 1; }

# --- python interpreter ------------------------------------------------
find_python() {
  local candidate
  for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c \
      'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
    then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

venv_python() {
  if [ -x "$VENV/bin/python" ]; then
    printf '%s' "$VENV/bin/python"
  elif [ -x "$VENV/Scripts/python.exe" ]; then
    printf '%s' "$VENV/Scripts/python.exe"
  else
    return 1
  fi
}

# --- virtual environment -----------------------------------------------
if ! venv_python >/dev/null 2>&1; then
  python_bin="$(find_python)" \
    || fail "Python 3.10+ not found. Install it from https://www.python.org/downloads/"
  echo "==> Creating $VENV with $python_bin"
  "$python_bin" -m venv "$VENV"
fi
py="$(venv_python)" || fail "$VENV looks broken. Delete it and run this script again."

# --- dependencies ------------------------------------------------------
if [ "$install" -eq 1 ]; then
  if [ -f "$LOCK" ] && cmp -s "$REQUIREMENTS" "$LOCK"; then
    echo "==> Dependencies already up to date"
  else
    echo "==> Installing $REQUIREMENTS"
    "$py" -m pip install --quiet --disable-pip-version-check -r "$REQUIREMENTS"
    cp "$REQUIREMENTS" "$LOCK"
  fi
fi

# --- configuration -----------------------------------------------------
env_value() { # file key
  sed -n "s/^[[:space:]]*$2[[:space:]]*=//p" "$1" | head -1 | tr -d '\r'
}

if [ ! -f .env ]; then
  cp .env.example .env
  fail ".env was missing, it has been created from .env.example. Fill it in and run this script again."
fi

missing=""
for key in $REQUIRED_KEYS; do
  value="$(env_value .env "$key")"
  example="$(env_value .env.example "$key")"
  if [ -z "$value" ] || { [ -n "$example" ] && [ "$value" = "$example" ]; }; then
    missing="$missing $key"
  fi
done
if [ -n "$missing" ]; then
  fail "these .env variables still hold their placeholder value:$missing"
fi

# --- run ---------------------------------------------------------------
echo "==> Starting the bot (Ctrl+C to stop)"
exec "$py" -m bot_horizon.main
