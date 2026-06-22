#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
MARKER="${VENV_DIR}/.deps_installed"

cd "${SCRIPT_DIR}"

# ── Create virtual environment if it doesn't exist ────────────────────────────
if [ ! -d "${VENV_DIR}" ]; then
    echo "[ setup ] Creating virtual environment…"
    python3 -m venv "${VENV_DIR}" || { echo "[ error ] Failed to create virtual environment."; exit 1; }
fi

# ── Activate ──────────────────────────────────────────────────────────────────
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

# ── Install / sync dependencies when requirements.txt is newer than marker ────
if [ ! -f "${MARKER}" ] || [ requirements.txt -nt "${MARKER}" ]; then
    echo "[ setup ] Installing / updating dependencies…"
    pip install --quiet -r requirements.txt || { echo "[ error ] Dependency installation failed."; exit 1; }
    echo "[ setup ] Downloading Camoufox browser…"
    camoufox fetch || { echo "[ error ] Camoufox fetch failed."; exit 1; }
    touch "${MARKER}"
fi

# ── Launch application ────────────────────────────────────────────────────────
echo "[ run ]   Starting FA & Inkbunny Downloader…"
python3 gui.py
